// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/security/Pausable.sol";
import "@aave/core-v3/contracts/flashloan/base/FlashLoanSimpleReceiverBase.sol";
import "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";

interface IMEVProtection {
    function checkTransaction(bytes memory txData) external view returns (bool);
}

// Add new interfaces
interface ICurvePool {
    function get_dy(int128 i, int128 j, uint256 dx) external view returns (uint256);
    function exchange(int128 i, int128 j, uint256 dx, uint256 min_dy) external;
}

interface IBalancerPool {
    function swap(
        bytes32 poolId,
        uint8 kind,
        address tokenIn,
        address tokenOut,
        uint256 amount,
        bytes memory userData
    ) external returns (uint256);
}

// Add after the existing interfaces
interface IUniswapV2Router {
    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory amounts);
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

contract AdvancedMultiDexArbitrage is 
    FlashLoanSimpleReceiverBase, 
    ReentrancyGuard, 
    Ownable, 
    Pausable 
{
    // Constants for gas optimization
    uint256 private constant MAX_SLIPPAGE = 200; // 2% max slippage
    uint256 private constant DEADLINE_BUFFER = 3; // 3 blocks deadline buffer
    
    // Router and factory addresses for all DEXes
    struct DEX {
        address router;
        address factory;
        string name;
        bool isActive;
    }
    
    mapping(bytes32 => DEX) public dexes;
    
    // Gas price threshold for execution
    uint256 public maxGasPrice;
    
    // MEV protection contract
    IMEVProtection public mevProtection;
    
    // Profit tracking
    struct ProfitTracker {
        uint256 totalExecutions;
        uint256 totalProfit;
        uint256 lastExecutionBlock;
        mapping(address => uint256) tokenProfits;
    }
    
    ProfitTracker public profitTracker;
    
    // Events
    event ArbitrageExecuted(
        address indexed token0,
        address indexed token1,
        uint256 profit,
        string sourcePool,
        string targetPool,
        uint256 gasUsed,
        uint256 gasPrice
    );
    
    event FlashLoanExecuted(
        address indexed token,
        uint256 amount,
        uint256 fee
    );
    
    constructor(
        address _addressProvider,
        address _mevProtection
    ) FlashLoanSimpleReceiverBase(IPoolAddressesProvider(_addressProvider)) {
        mevProtection = IMEVProtection(_mevProtection);
        maxGasPrice = 100 gwei;
    }
    
    // DEX management functions
    function addDex(
        string memory name,
        address router,
        address factory
    ) external onlyOwner {
        bytes32 dexId = keccak256(abi.encodePacked(name));
        dexes[dexId] = DEX(router, factory, name, true);
    }
    
    function toggleDex(string memory name) external onlyOwner {
        bytes32 dexId = keccak256(abi.encodePacked(name));
        require(dexes[dexId].router != address(0), "DEX not found");
        dexes[dexId].isActive = !dexes[dexId].isActive;
    }
    
    // Flash loan execution
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        // Decode params
        (
            address token0,
            address token1,
            string memory sourceDex,
            string memory targetDex,
            uint256 minProfit
        ) = abi.decode(params, (address, address, string, string, uint256));
        
        // Execute arbitrage with flash loaned funds
        uint256 startBalance = IERC20(asset).balanceOf(address(this));
        executeArbitrageTrade(token0, token1, amount, sourceDex, targetDex);
        uint256 endBalance = IERC20(asset).balanceOf(address(this));
        
        // Verify profit
        uint256 profit = endBalance - startBalance;
        require(profit >= premium + minProfit, "Insufficient profit");
        
        // Approve repayment
        IERC20(asset).approve(address(POOL), amount + premium);
        
        emit FlashLoanExecuted(asset, amount, premium);
        return true;
    }
    
    // Main arbitrage execution function
    function executeArbitrage(
        address token0,
        address token1,
        uint256 amount,
        string calldata sourceDex,
        string calldata targetDex,
        uint256 minProfit
    ) external nonReentrant whenNotPaused onlyOwner {
        // Check gas price
        require(tx.gasprice <= maxGasPrice, "Gas price too high");
        
        // Check MEV protection
        require(
            mevProtection.checkTransaction(msg.data),
            "MEV protection check failed"
        );
        
        // Calculate optimal path and amounts
        (
            uint256 optimalAmount,
            uint256 expectedProfit
        ) = calculateOptimalTrade(token0, token1, amount, sourceDex, targetDex);
        
        require(expectedProfit >= minProfit, "Insufficient expected profit");
        
        // Execute flash loan
        bytes memory params = abi.encode(
            token0,
            token1,
            sourceDex,
            targetDex,
            minProfit
        );
        
        POOL.flashLoanSimple(
            address(this),
            token0,
            optimalAmount,
            params,
            0
        );
        
        // Update profit tracker
        updateProfitTracker(token1, expectedProfit);
    }
    
    // Internal execution functions
    function executeArbitrageTrade(
        address token0,
        address token1,
        uint256 amount,
        string memory sourceDex,
        string memory targetDex
    ) internal {
        // Get DEX information
        bytes32 sourceDexId = keccak256(abi.encodePacked(sourceDex));
        bytes32 targetDexId = keccak256(abi.encodePacked(targetDex));
        
        require(dexes[sourceDexId].isActive, "Source DEX not active");
        require(dexes[targetDexId].isActive, "Target DEX not active");
        
        // Execute trades with slippage protection
        uint256 amountOut = executeTradeWithSlippage(
            token0,
            token1,
            amount,
            dexes[sourceDexId]
        );
        
        executeTradeWithSlippage(
            token1,
            token0,
            amountOut,
            dexes[targetDexId]
        );
    }
    
    function executeTradeWithSlippage(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        DEX memory dex
    ) internal returns (uint256) {
        // Calculate minimum output with slippage
        uint256 expectedOut = getExpectedOutput(
            tokenIn,
            tokenOut,
            amountIn,
            dex
        );
        
        uint256 minOut = expectedOut * (10000 - MAX_SLIPPAGE) / 10000;
        
        // Execute trade
        return executeTrade(
            tokenIn,
            tokenOut,
            amountIn,
            minOut,
            dex
        );
    }
    
    // Add new functions
    function executeCurveSwap(
        address pool,
        int128 i,
        int128 j,
        uint256 amount,
        uint256 minReturn
    ) internal returns (uint256) {
        ICurvePool(pool).exchange(i, j, amount, minReturn);
        return IERC20(pool).balanceOf(address(this));
    }

    function executeBalancerSwap(
        address pool,
        bytes32 poolId,
        address tokenIn,
        address tokenOut,
        uint256 amount
    ) internal returns (uint256) {
        return IBalancerPool(pool).swap(
            poolId,
            0, // GIVEN_IN
            tokenIn,
            tokenOut,
            amount,
            ""
        );
    }
    
    // Add these functions to the main contract
    function getExpectedOutput(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        DEX memory dex
    ) internal view returns (uint256) {
        if (bytes(dex.name).length == 0) return 0;
        
        if (_isCurveDex(dex.name)) {
            return ICurvePool(dex.router).get_dy(0, 1, amountIn);
        } else if (_isBalancerDex(dex.name)) {
            // Balancer specific price calculation
            return _getBalancerOutput(dex.router, tokenIn, tokenOut, amountIn);
        } else {
            // Standard UniswapV2 style DEX
            address[] memory path = new address[](2);
            path[0] = tokenIn;
            path[1] = tokenOut;
            uint256[] memory amounts = IUniswapV2Router(dex.router).getAmountsOut(amountIn, path);
            return amounts[1];
        }
    }

    function executeTrade(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        DEX memory dex
    ) internal returns (uint256) {
        if (_isCurveDex(dex.name)) {
            return executeCurveSwap(dex.router, 0, 1, amountIn, minOut);
        } else if (_isBalancerDex(dex.name)) {
            bytes32 poolId = IBalancerPool(dex.router).getPoolId();
            return executeBalancerSwap(dex.router, poolId, tokenIn, tokenOut, amountIn);
        } else {
            address[] memory path = new address[](2);
            path[0] = tokenIn;
            path[1] = tokenOut;
            
            IERC20(tokenIn).approve(dex.router, amountIn);
            
            uint256[] memory amounts = IUniswapV2Router(dex.router).swapExactTokensForTokens(
                amountIn,
                minOut,
                path,
                address(this),
                block.timestamp + DEADLINE_BUFFER
            );
            return amounts[1];
        }
    }

    function simulateTrade(
        address token0,
        address token1,
        uint256 amount,
        string memory sourceDex,
        string memory targetDex
    ) internal view returns (uint256 profit, uint256 gasEstimate) {
        bytes32 sourceDexId = keccak256(abi.encodePacked(sourceDex));
        bytes32 targetDexId = keccak256(abi.encodePacked(targetDex));
        
        DEX memory sourceDexInfo = dexes[sourceDexId];
        DEX memory targetDexInfo = dexes[targetDexId];
        
        uint256 firstTradeOutput = getExpectedOutput(token0, token1, amount, sourceDexInfo);
        uint256 secondTradeOutput = getExpectedOutput(token1, token0, firstTradeOutput, targetDexInfo);
        
        if (secondTradeOutput > amount) {
            profit = secondTradeOutput - amount;
            gasEstimate = 300000; // Base estimate, can be refined
        }
    }

    // Helper functions
    function _isCurveDex(string memory name) internal pure returns (bool) {
        return keccak256(bytes(name)) == keccak256(bytes("curve"));
    }
    
    function _isBalancerDex(string memory name) internal pure returns (bool) {
        return keccak256(bytes(name)) == keccak256(bytes("balancer"));
    }

    function _getBalancerOutput(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn
    ) internal view returns (uint256) {
        // Implement Balancer-specific price checking logic
        // This will depend on the Balancer pool type and configuration
        return 0; // Placeholder - implement actual calculation
    }

    function calculateOptimalTrade(
        address token0,
        address token1,
        uint256 maxAmount,
        string memory sourceDex,
        string memory targetDex
    ) internal view returns (uint256 optimalAmount, uint256 expectedProfit) {
        // Binary search for optimal amount
        uint256 low = 0;
        uint256 high = maxAmount;
        
        while (low < high) {
            uint256 mid = (low + high) / 2;
            (uint256 profit,) = simulateTrade(
                token0,
                token1,
                mid,
                sourceDex,
                targetDex
            );
            
            if (profit > expectedProfit) {
                expectedProfit = profit;
                optimalAmount = mid;
                low = mid + 1;
            } else {
                high = mid - 1;
            }
        }
    }
    
    function updateProfitTracker(address token, uint256 profit) internal {
        profitTracker.totalExecutions++;
        profitTracker.totalProfit += profit;
        profitTracker.lastExecutionBlock = block.number;
        profitTracker.tokenProfits[token] += profit;
    }
    
    // Admin functions
    function setMaxGasPrice(uint256 _maxGasPrice) external onlyOwner {
        maxGasPrice = _maxGasPrice;
    }
    
    function setMEVProtection(address _mevProtection) external onlyOwner {
        mevProtection = IMEVProtection(_mevProtection);
    }
    
    function pause() external onlyOwner {
        _pause();
    }
    
    function unpause() external onlyOwner {
        _unpause();
    }
    
    // Emergency functions
    function rescueTokens(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner(), balance);
    }
    
    receive() external payable {}
}