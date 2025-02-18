import asyncio
import logging
from typing import Dict, List, Tuple, Optional
from web3 import Web3
from eth_typing import Address
from decimal import Decimal
from web3.contract import Contract
from web3.middleware import geth_poa_middleware
import json
import time
from concurrent.futures import ThreadPoolExecutor
from eth_account import Account
from dataclasses import dataclass
import aiohttp
import numpy as np
import os

@dataclass
class ArbitrageOpportunity:
    token0: Address
    token1: Address
    source_dex: str
    target_dex: str
    amount_in: int
    expected_profit: Decimal
    execution_path: List[str]
    gas_estimate: int

class ArbitrageBot:
    def __init__(self, config: dict):
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(config['node_url']))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        
        # Load contracts
        self.arbitrage_contract = self._load_contract(
            config['arbitrage_contract_address'],
            'AdvancedMultiDexArbitrage.json'
        )
        
        # Initialize DEX interfaces
        self.dex_interfaces = self._initialize_dex_interfaces()
        
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # Initialize price cache
        self.price_cache = {}
        self.last_cache_update = 0
        self.CACHE_DURATION = 5  # 5 seconds cache
        
        # Initialize token pairs to monitor
        self.token_pairs = self._load_token_pairs()
        
        # Gas price monitoring
        self.max_gas_price = Web3.toWei(100, 'gwei')
        
        # Profit threshold
        self.min_profit_threshold = Decimal('0.01')  # 1% minimum profit
        
        # Flash loan parameters
        self.flash_loan_fee = Decimal('0.0009')  # 0.09% AAVE flash loan fee
        
        # Initialize MEV protection
        self.flashbots_endpoint = config['flashbots_endpoint']
        self.private_key = config['private_key']
        self.account = Account.from_key(self.private_key)
        
        # Initialize statistics tracking
        self.stats = {
            'opportunities_found': 0,
            'trades_executed': 0,
            'total_profit': Decimal('0'),
            'failed_trades': 0
        }

    def _initialize_dex_interfaces(self) -> Dict[str, Contract]:
        """Initialize interfaces for all supported DEXes"""
        interfaces = {}
        
        for dex_name, dex_config in self.config['dexes'].items():
            try:
                # Load ABI from file
                abi_path = os.path.join('abis', dex_config['abi_file'])
                with open(abi_path, 'r') as f:
                    abi = json.load(f)
                
                # Create contract interface
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(dex_config['router']),
                    abi=abi
                )
                
                interfaces[dex_name] = contract
                logging.info(f"Initialized {dex_name} interface")
                
            except Exception as e:
                logging.error(f"Failed to initialize {dex_name}: {str(e)}")
                continue
        
        return interfaces

    def _load_token_pairs(self) -> List[Tuple[Address, Address]]:
        """Load token pairs from config"""
        return [
            (
                Web3.to_checksum_address(pair['token0']),
                Web3.to_checksum_address(pair['token1'])
            )
            for pair in self.config['token_pairs']
        ]

    def _calculate_profit(
        self,
        amount: int,
        source_price: Decimal,
        target_price: Decimal
    ) -> Decimal:
        """Calculate potential profit for a trade"""
        return (target_price - source_price) * Decimal(str(amount))

    async def _estimate_gas_cost(
        self,
        token0: Address,
        token1: Address,
        amount: int,
        source_dex: str,
        target_dex: str
    ) -> int:
        """Estimate gas cost for arbitrage execution"""
        try:
            # Add 20% buffer to base gas estimate
            base_estimate = 300000  # Base gas estimate for flash loan + 2 swaps
            return int(base_estimate * 1.2)  # 20% safety margin
            
        except Exception as e:
            logging.error(f"Error estimating gas: {str(e)}")
            return 500000  # Conservative fallback estimate

    async def _check_all_opportunities(self) -> List[ArbitrageOpportunity]:
        """Check all token pairs across all DEXes for arbitrage opportunities"""
        opportunities = []
        
        async with aiohttp.ClientSession() as session:
            tasks = []
            for token0, token1 in self.token_pairs:
                task = self._check_pair_opportunities(session, token0, token1)
                tasks.append(task)
            
            results = await asyncio.gather(*tasks)
            for result in results:
                if result:
                    opportunities.extend(result)
        
        return opportunities

    async def _check_pair_opportunities(
        self,
        session: aiohttp.ClientSession,
        token0: Address,
        token1: Address
    ) -> List[ArbitrageOpportunity]:
        """Check single token pair across all DEXes"""
        opportunities = []
        prices = await self._get_prices(session, token0, token1)
        
        for source_dex in self.dex_interfaces:
            for target_dex in self.dex_interfaces:
                if source_dex != target_dex:
                    opportunity = await self._analyze_opportunity(
                        token0,
                        token1,
                        source_dex,
                        target_dex,
                        prices
                    )
                    if opportunity:
                        opportunities.append(opportunity)
        
        return opportunities

    async def _get_prices(
        self,
        session: aiohttp.ClientSession,
        token0: Address,
        token1: Address
    ) -> Dict[str, Decimal]:
        """Get current prices from all DEXes"""
        current_time = time.time()
        cache_key = f"{token0}-{token1}"
        
        # Return cached prices if valid
        if (cache_key in self.price_cache and 
            current_time - self.last_cache_update < self.CACHE_DURATION):
            return self.price_cache[cache_key]
        
        prices = {}
        tasks = []
        
        for dex_name, dex_interface in self.dex_interfaces.items():
            task = self._get_dex_price(session, dex_name, dex_interface, token0, token1)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks)
        for dex_name, price in zip(self.dex_interfaces.keys(), results):
            if price:
                prices[dex_name] = price
        
        self.price_cache[cache_key] = prices
        self.last_cache_update = current_time
        
        return prices

    async def _get_dex_price(
        self,
        session: aiohttp.ClientSession,
        dex_name: str,
        dex_interface: Contract,
        token0: Address,
        token1: Address
    ) -> Optional[Decimal]:
        """Get price from specific DEX with error handling"""
        try:
            if dex_name == 'curve':
                return await self._get_curve_price(session, token0, token1)
            elif dex_name == 'balancer':
                return await self._get_balancer_price(session, token0, token1)
            else:
                # Standard DEX price query
                amount_in = Web3.toWei(1, 'ether')
                amounts = await dex_interface.functions.getAmountsOut(
                    amount_in, [token0, token1]
                ).call()
                return Decimal(str(amounts[1])) / Decimal(str(amounts[0]))
                
        except Exception as e:
            logging.error(f"Error getting {dex_name} price: {str(e)}")
            return None

    async def _get_curve_price(
        self,
        session: aiohttp.ClientSession,
        token0: Address,
        token1: Address
    ) -> Optional[Decimal]:
        """Get price from Curve pools"""
        try:
            # Curve-specific price calculation
            pool_address = self.curve_registry.get_pool(token0, token1)
            pool_contract = self.w3.eth.contract(
                address=pool_address,
                abi=self.curve_pool_abi
            )
            # Get price from Curve pool
            dy = await pool_contract.functions.get_dy(0, 1, Web3.toWei(1, 'ether')).call()
            return Decimal(str(dy)) / Decimal(str(Web3.toWei(1, 'ether')))
        except Exception as e:
            logging.error(f"Error getting Curve price: {str(e)}")
            return None

    def _load_contract(self, address: str, abi_file: str) -> Contract:
        """Load contract with error handling"""
        try:
            with open(os.path.join('abis', abi_file)) as f:
                abi = json.load(f)
            return self.w3.eth.contract(
                address=Web3.to_checksum_address(address),
                abi=abi
            )
        except Exception as e:
            logging.error(f"Error loading contract: {str(e)}")
            raise

    async def _analyze_opportunity(
        self,
        token0: Address,
        token1: Address,
        source_dex: str,
        target_dex: str,
        prices: Dict[str, Decimal]
    ) -> Optional[ArbitrageOpportunity]:
        """Analyze potential arbitrage opportunity between two DEXes"""
        try:
            source_price = prices[source_dex]
            target_price = prices[target_dex]
            
            # Calculate optimal trade size using binary search
            optimal_amount = await self._find_optimal_amount(
                token0,
                token1,
                source_dex,
                target_dex,
                source_price,
                target_price
            )
            
            if not optimal_amount:
                return None
            
            # Calculate expected profit
            profit = self._calculate_profit(
                optimal_amount,
                source_price,
                target_price
            )
            
            # Estimate gas costs
            gas_estimate = await self._estimate_gas_cost(
                token0,
                token1,
                optimal_amount,
                source_dex,
                target_dex
            )
            
            # Calculate net profit after gas and flash loan fees
            flash_loan_cost = Decimal(str(optimal_amount)) * self.flash_loan_fee
            gas_cost = Decimal(str(gas_estimate * self.w3.eth.gas_price))
            net_profit = profit - flash_loan_cost - gas_cost
            
            if net_profit > self.min_profit_threshold:
                return ArbitrageOpportunity(
                    token0=token0,
                    token1=token1,
                    source_dex=source_dex,
                    target_dex=target_dex,
                    amount_in=optimal_amount,
                    expected_profit=net_profit,
                    execution_path=[source_dex, target_dex],
                    gas_estimate=gas_estimate
                )
            
        except Exception as e:
            logging.error(f"Error analyzing opportunity: {str(e)}")
        
        return None

    async def _execute_arbitrage(self, opportunity: ArbitrageOpportunity):
        """Execute arbitrage opportunity using flash loans"""
        try:
            # Prepare transaction
            tx_params = await self._prepare_flashbots_bundle(opportunity)
            
            # Sign transaction
            signed_tx = self.w3.eth.account.sign_transaction(
                tx_params,
                self.private_key
            )
            
            # Submit to Flashbots
            success = await self._submit_to_flashbots(signed_tx)
            
            if success:
                self.stats['trades_executed'] += 1
                self.stats['total_profit'] += opportunity.expected_profit
                logging.info(
                    f"Successfully executed arbitrage: {opportunity.expected_profit} profit"
                )
            else:
                self.stats['failed_trades'] += 1
                logging.warning("Failed to execute arbitrage through Flashbots")
                
        except Exception as e:
            self.stats['failed_trades'] += 1
            logging.error(f"Error executing arbitrage: {str(e)}")

    async def _prepare_flashbots_bundle(
        self,
        opportunity: ArbitrageOpportunity
    ) -> dict:
        """Prepare transaction bundle for Flashbots"""
        nonce = self.w3.eth.get_transaction_count(self.account.address)
        
        tx_params = {
            'from': self.account.address,
            'to': self.arbitrage_contract.address,
            'nonce': nonce,
            'gas': opportunity.gas_estimate,
            'maxFeePerGas': self.w3.eth.gas_price,
            'maxPriorityFeePerGas': self.w3.eth.gas_price,
            'data': self.arbitrage_contract.encodeABI(
                fn_name='executeArbitrage',
                args=[
                    opportunity.token0,
                    opportunity.token1,
                    opportunity.amount_in,
                    opportunity.source_dex,
                    opportunity.target_dex,
                    int(opportunity.expected_profit)
                ]
            )
        }
        
        return tx_params

    async def _submit_to_flashbots(self, signed_tx) -> bool:
        """Submit transaction bundle to Flashbots"""
        async with aiohttp.ClientSession() as session:
            try:
                response = await session.post(
                    self.flashbots_endpoint,
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'eth_sendBundle',
                        'params': [
                            {
                                'txs': [signed_tx.rawTransaction.hex()],
                                'blockNumber': hex(self.w3.eth.block_number + 1),
                                'minTimestamp': 0,
                                'maxTimestamp': int(time.time()) + 120,
                            }
                        ]
                    }
                )
                
                result = await response.json()
                return 'result' in result and result['result'] is not None
                
            except Exception as e:
                logging.error(f"Error submitting to Flashbots: {str(e)}")
                return False

    async def _find_optimal_amount(
        self,
        token0: Address,
        token1: Address,
        source_dex: str,
        target_dex: str,
        source_price: Decimal,
        target_price: Decimal
    ) -> Optional[int]:
        """Find optimal trade amount using binary search"""
        try:
            # Define search range
            min_amount = Web3.toWei(0.1, 'ether')  # Minimum trade size
            max_amount = Web3.toWei(100, 'ether')  # Maximum trade size
            best_amount = None
            best_profit = Decimal('0')
            
            for _ in range(20):  # Binary search iterations
                mid_amount = (min_amount + max_amount) // 2
                
                # Calculate potential profit at this amount
                profit = await self._simulate_trade_profit(
                    token0,
                    token1,
                    mid_amount,
                    source_dex,
                    target_dex
                )
                
                if profit > best_profit:
                    best_profit = profit
                    best_amount = mid_amount
                
                # Adjust search range
                lower_profit = await self._simulate_trade_profit(
                    token0,
                    token1,
                    mid_amount - Web3.toWei(0.1, 'ether'),
                    source_dex,
                    target_dex
                )
                
                if lower_profit < profit:
                    min_amount = mid_amount
                else:
                    max_amount = mid_amount
            
            return best_amount
            
        except Exception as e:
            logging.error(f"Error finding optimal amount: {str(e)}")
            return None

    async def _simulate_trade_profit(
        self,
        token0: Address,
        token1: Address,
        amount: int,
        source_dex: str,
        target_dex: str
    ) -> Decimal:
        """Simulate trade to calculate potential profit"""
        try:
            # Get source DEX output
            source_output = await self._get_dex_output(
                source_dex,
                token0,
                token1,
                amount
            )
            
            # Get target DEX output
            target_output = await self._get_dex_output(
                target_dex,
                token1,
                token0,
                source_output
            )
            
            # Calculate profit
            profit = Decimal(str(target_output - amount))
            
            # Subtract flash loan fee
            flash_loan_fee = Decimal(str(amount)) * self.flash_loan_fee
            profit -= flash_loan_fee
            
            return profit
            
        except Exception as e:
            logging.error(f"Error simulating trade profit: {str(e)}")
            return Decimal('0')

    async def _get_dex_output(
        self,
        dex_name: str,
        token_in: Address,
        token_out: Address,
        amount_in: int
    ) -> int:
        """Get expected output amount from a DEX"""
        try:
            dex_interface = self.dex_interfaces[dex_name]
            
            # Get path for swap
            path = [token_in, token_out]
            
            # Call getAmountsOut function
            amounts = await dex_interface.functions.getAmountsOut(
                amount_in,
                path
            ).call()
            
            return amounts[-1]
            
        except Exception as e:
            logging.error(f"Error getting DEX output: {str(e)}")
            return 0

    def _validate_opportunity(self, opportunity: ArbitrageOpportunity) -> bool:
        """Validate if an arbitrage opportunity should be executed"""
        try:
            # Check minimum profit threshold
            if opportunity.expected_profit <= self.min_profit_threshold:
                return False
                
            # Check gas price is still favorable
            current_gas_price = self.w3.eth.gas_price
            estimated_gas_cost = Decimal(str(opportunity.gas_estimate * current_gas_price))
            if estimated_gas_cost >= opportunity.expected_profit:
                return False
                
            # Check if prices haven't changed significantly
            current_prices = await self._get_prices(
                aiohttp.ClientSession(),
                opportunity.token0,
                opportunity.token1
            )
            if not self._verify_prices(current_prices, opportunity):
                return False
                
            return True
            
        except Exception as e:
            logging.error(f"Error validating opportunity: {str(e)}")
            return False
            
    def _verify_prices(self, current_prices: Dict[str, Decimal], 
                      opportunity: ArbitrageOpportunity) -> bool:
        """Verify prices haven't changed significantly"""
        # Allow 1% price deviation
        MAX_PRICE_DEVIATION = Decimal('0.01')
        
        try:
            source_price = current_prices[opportunity.source_dex]
            target_price = current_prices[opportunity.target_dex]
            
            # Calculate price deviation
            source_deviation = abs(source_price - self.price_cache[f"{opportunity.token0}-{opportunity.token1}"][opportunity.source_dex]) / source_price
            target_deviation = abs(target_price - self.price_cache[f"{opportunity.token0}-{opportunity.token1}"][opportunity.target_dex]) / target_price
            
            return source_deviation <= MAX_PRICE_DEVIATION and target_deviation <= MAX_PRICE_DEVIATION
            
        except Exception as e:
            logging.error(f"Error verifying prices: {str(e)}")
            return False

    async def monitor_prices(self):
        """Main price monitoring loop"""
        logging.info("Starting price monitoring...")
        while True:
            try:
                current_gas_price = self.w3.eth.gas_price
                if current_gas_price > self.max_gas_price:
                    logging.info(f"Gas price too high: {current_gas_price}")
                    await asyncio.sleep(10)
                    continue
                
                opportunities = await self._check_all_opportunities()
                for opp in opportunities:
                    if self._validate_opportunity(opp):
                        await self._execute_arbitrage(opp)
                
                await asyncio.sleep(1)  # Rate limiting
                
            except Exception as e:
                logging.error(f"Error in monitoring loop: {str(e)}")
                await asyncio.sleep(5)  # Wait before retrying

    def run(self):
        """Main bot execution loop"""
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self.monitor_prices())
        except KeyboardInterrupt:
            logging.info("Shutting down bot...")
        finally:
            loop.close()

if __name__ == "__main__":
    # Load configuration
    with open("config.json") as f:
        config = json.load(f)
    
    # Initialize and run bot
    bot = ArbitrageBot(config)
    bot.run()