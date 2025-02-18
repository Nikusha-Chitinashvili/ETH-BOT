import json
import os
from typing import Dict, List, Any
from web3 import Web3
from dotenv import load_dotenv
from dataclasses import dataclass

@dataclass
class DexConfig:
    router: str
    factory: str
    abi_file: str

@dataclass
class TokenPair:
    token0: str
    token1: str

@dataclass
class GasSettings:
    max_gas_price_gwei: int
    priority_fee_gwei: int

@dataclass
class ProfitSettings:
    min_profit_threshold_percent: float
    min_profit_usd: float

@dataclass
class MonitoringSettings:
    price_cache_duration_seconds: int
    check_interval_ms: int
    gas_estimate_buffer_percent: int

class Config:
    def __init__(self, config_path: str):
        load_dotenv()
        
        with open(config_path, 'r') as f:
            self._config = json.load(f)
        
        self.validate_config()
        
        # Node connection
        self.node_url = self._get_env_or_config('NODE_URL', 'node_url')
        self.arbitrage_contract_address = Web3.to_checksum_address(
            self._config['arbitrage_contract_address']
        )
        
        # MEV Protection
        self.flashbots_endpoint = self._config['flashbots_endpoint']
        self.private_key = self._get_env_or_config('PRIVATE_KEY', 'private_key')
        
        # DEX configurations
        self.dexes: Dict[str, DexConfig] = {
            name: DexConfig(
                router=Web3.to_checksum_address(data['router']),
                factory=Web3.to_checksum_address(data['factory']),
                abi_file=data['abi_file']
            )
            for name, data in self._config['dexes'].items()
        }
        
        # Token pairs
        self.token_pairs: List[TokenPair] = [
            TokenPair(
                token0=Web3.to_checksum_address(pair['token0']),
                token1=Web3.to_checksum_address(pair['token1'])
            )
            for pair in self._config['token_pairs']
        ]
        
        # Settings
        self.gas_settings = GasSettings(**self._config['gas_price_settings'])
        self.profit_settings = ProfitSettings(**self._config['profit_settings'])
        self.monitoring_settings = MonitoringSettings(**self._config['monitoring_settings'])
        
        # Logging configuration
        self.log_level = self._config['logging']['level']
        self.log_file = self._config['logging']['file']

    def _get_env_or_config(self, env_key: str, config_key: str) -> str:
        """Get value from environment variable or config file"""
        return os.getenv(env_key) or self._config[config_key]

    def validate_config(self) -> None:
        """Validate required configuration fields"""
        required_fields = [
            'node_url',
            'arbitrage_contract_address',
            'flashbots_endpoint',
            'private_key',
            'dexes',
            'token_pairs',
            'gas_price_settings',
            'profit_settings',
            'monitoring_settings',
            'logging'
        ]
        
        missing_fields = [
            field for field in required_fields 
            if field not in self._config
        ]
        
        if missing_fields:
            raise ValueError(
                f"Missing required configuration fields: {', '.join(missing_fields)}"
            )

    def get_dex_abi_path(self, dex_name: str) -> str:
        """Get the full path to a DEX's ABI file"""
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'abis',
            self.dexes[dex_name].abi_file
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary (for logging/debugging)"""
        return {
            'node_url': self.node_url,
            'arbitrage_contract_address': self.arbitrage_contract_address,
            'flashbots_endpoint': self.flashbots_endpoint,
            'dexes': {
                name: {
                    'router': dex.router,
                    'factory': dex.factory,
                    'abi_file': dex.abi_file
                }
                for name, dex in self.dexes.items()
            },
            'token_pairs': [
                {'token0': pair.token0, 'token1': pair.token1}
                for pair in self.token_pairs
            ],
            'gas_settings': self.gas_settings.__dict__,
            'profit_settings': self.profit_settings.__dict__,
            'monitoring_settings': self.monitoring_settings.__dict__,
            'logging': {
                'level': self.log_level,
                'file': self.log_file
            }
        }
