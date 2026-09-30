from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

load_dotenv()


# Keep operational defaults in one place so the environment mapping below is
# easy to audit. Credentials intentionally have no defaults other than empty
# strings; they must come from the process environment (or an environment
# file loaded into it by python-dotenv).
_DEFAULTS: Final = {
    "log_level": "INFO",
    "dry_run": True,
    "clob_api_url": "https://clob.polymarket.com",
    "gamma_api_url": "https://gamma-api.polymarket.com",
    "chain_id": 137,
    "signature_type": 1,
    "poll_interval_seconds": 3.0,
    "market_scan_interval_seconds": 300.0,
    "order_ttl_seconds": 15.0,
    "quote_refresh_seconds": 15.0,
    "inventory_quote_expiration_seconds": 35.0,
    "gtd_min_expiration_seconds": 240.0,
    "min_resolution_hours": 1.0,
    "min_price": 0.05,
    "max_price": 0.95,
    "min_spread": 0.01,
    "max_spread": 0.04,
    "percentile_low": 40.0,
    "percentile_high": 70.0,
    "scanner_request_limit": 1000,
    "scanner_min_book_depth": 1,
    "order_size": 5.0,
    "max_position_per_side": 10.0,
    "max_open_orders": 10,
    "max_total_capital": 100.0,
    "sanity_test_price": 0.01,
    "sanity_test_size": 5.0,
    "movement_threshold": 0.03,
    "yellow_snapshots": 2,
    "inventory_threshold": 5.0,
    "min_quote_spread": 0.01,
    "min_volume_24h": 1000.0,
    "max_volume_24h": 100000.0,
    "min_liquidity": 1000.0,
    "inventory_refresh_seconds": 30.0,
    "scan_retry_seconds": 30.0,
    "max_consecutive_errors": 5,
    "settlement_timeout_seconds": 120.0,
    "stop_loss_distance": 0.30,
    "min_pair_profit": 0.01,
}

_REQUIRED_ORDER_SIZE: Final = 5.0
_MAX_YELLOW_SNAPSHOTS: Final = 4
_MAX_HEARTBEAT_INTERVAL_SECONDS: Final = 5.0


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Config:
    log_level: str
    dry_run: bool
    pk: str
    funder: str
    clob_api_key: str
    clob_secret: str
    clob_passphrase: str
    token_id: str
    clob_api_url: str
    gamma_api_url: str
    chain_id: int
    signature_type: int
    poll_interval_seconds: float
    market_scan_interval_seconds: float
    order_ttl_seconds: float
    quote_refresh_seconds: float
    min_resolution_hours: float
    min_price: float
    max_price: float
    min_spread: float
    max_spread: float
    percentile_low: float
    percentile_high: float
    scanner_request_limit: int
    scanner_min_book_depth: int
    order_size: float
    max_position_per_side: float
    max_open_orders: int
    max_total_capital: float
    sanity_test_price: float
    sanity_test_size: float
    movement_threshold: float
    yellow_snapshots: int
    inventory_threshold: float
    min_quote_spread: float
    min_volume_24h: float
    max_volume_24h: float
    min_liquidity: float
    inventory_refresh_seconds: float
    scan_retry_seconds: float
    max_consecutive_errors: int
    settlement_timeout_seconds: float
    stop_loss_distance: float = _DEFAULTS["stop_loss_distance"]
    min_pair_profit: float = _DEFAULTS["min_pair_profit"]
    inventory_quote_expiration_seconds: float = _DEFAULTS["inventory_quote_expiration_seconds"]
    # CLOB currently rejects GTD timestamps that are not strictly >180 seconds ahead.
    # Leave room for request latency and CLOB clock skew above its >180s limit.
    gtd_min_expiration_seconds: float = _DEFAULTS["gtd_min_expiration_seconds"]

    def __post_init__(self) -> None:
        if not 0 < self.stop_loss_distance <= 1 or not 0 <= self.min_pair_profit < 1:
            raise ValueError("Invalid STOP_LOSS_DISTANCE or MIN_PAIR_PROFIT")
        for name, value in vars(self).items():
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{name} must be finite and non-negative")
        if self.order_size != _REQUIRED_ORDER_SIZE or self.sanity_test_size != _REQUIRED_ORDER_SIZE:
            raise ValueError("V1 requires ORDER_SIZE=5 and SANITY_TEST_SIZE=5")
        if not 0 < self.min_price < self.max_price < 1:
            raise ValueError("Require 0 < MIN_PRICE < MAX_PRICE < 1")
        if (not 0 < self.min_spread <= self.max_spread < 1
                or not 0 < self.min_quote_spread <= self.max_spread):
            raise ValueError("Invalid spread limits")
        if not 0 <= self.percentile_low < self.percentile_high <= 100:
            raise ValueError("Invalid volume percentiles")
        if not 0 < self.min_volume_24h <= self.max_volume_24h:
            raise ValueError("Invalid volume limits")
        if not 0 < self.inventory_threshold <= self.max_position_per_side:
            raise ValueError("Invalid inventory thresholds")
        if self.max_position_per_side < _REQUIRED_ORDER_SIZE or self.max_open_orders < 1:
            raise ValueError("Position limit must allow 5 shares; order limit must be positive")
        for name in ("poll_interval_seconds", "market_scan_interval_seconds", "order_ttl_seconds",
                     "quote_refresh_seconds", "inventory_quote_expiration_seconds", "gtd_min_expiration_seconds",
                     "inventory_refresh_seconds", "scan_retry_seconds", "movement_threshold",
                     "max_total_capital", "settlement_timeout_seconds", "max_consecutive_errors",
                     "scanner_request_limit", "scanner_min_book_depth"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 1 <= self.yellow_snapshots <= _MAX_YELLOW_SNAPSHOTS:
            raise ValueError("YELLOW_SNAPSHOTS must be between 1 and 4")
        if self.poll_interval_seconds > _MAX_HEARTBEAT_INTERVAL_SECONDS:
            raise ValueError("POLL_INTERVAL_SECONDS must be <= 5 for the order heartbeat")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            log_level=os.getenv("LOG_LEVEL", _DEFAULTS["log_level"]),
            dry_run=_bool("DRY_RUN", _DEFAULTS["dry_run"]),
            pk=os.getenv("PK", ""),
            funder=os.getenv("FUNDER", ""),
            clob_api_key=os.getenv("CLOB_API_KEY", ""),
            clob_secret=os.getenv("CLOB_SECRET", ""),
            clob_passphrase=os.getenv("CLOB_PASS_PHRASE", ""),
            token_id=os.getenv("TOKEN_ID", ""),
            clob_api_url=os.getenv("CLOB_API_URL", _DEFAULTS["clob_api_url"]),
            gamma_api_url=os.getenv("GAMMA_API_URL", _DEFAULTS["gamma_api_url"]),
            chain_id=_int("CHAIN_ID", _DEFAULTS["chain_id"]),
            signature_type=_int("SIGNATURE_TYPE", _DEFAULTS["signature_type"]),
            poll_interval_seconds=_float("POLL_INTERVAL_SECONDS", _DEFAULTS["poll_interval_seconds"]),
            market_scan_interval_seconds=_float("MARKET_SCAN_INTERVAL_SECONDS", _DEFAULTS["market_scan_interval_seconds"]),
            order_ttl_seconds=_float("ORDER_TTL_SECONDS", _DEFAULTS["order_ttl_seconds"]),
            quote_refresh_seconds=_float("QUOTE_REFRESH_SECONDS", _DEFAULTS["quote_refresh_seconds"]),
            inventory_quote_expiration_seconds=_float("INVENTORY_QUOTE_EXPIRATION_SECONDS", _DEFAULTS["inventory_quote_expiration_seconds"]),
            gtd_min_expiration_seconds=_float("GTD_MIN_EXPIRATION_SECONDS", _DEFAULTS["gtd_min_expiration_seconds"]),
            min_resolution_hours=_float("MIN_RESOLUTION_HOURS", _DEFAULTS["min_resolution_hours"]),
            min_price=_float("MIN_PRICE", _DEFAULTS["min_price"]),
            max_price=_float("MAX_PRICE", _DEFAULTS["max_price"]),
            min_spread=_float("MIN_SPREAD", _DEFAULTS["min_spread"]),
            max_spread=_float("MAX_SPREAD", _DEFAULTS["max_spread"]),
            percentile_low=_float("PERCENTILE_LOW", _DEFAULTS["percentile_low"]),
            percentile_high=_float("PERCENTILE_HIGH", _DEFAULTS["percentile_high"]),
            scanner_request_limit=_int("SCANNER_REQUEST_LIMIT", _DEFAULTS["scanner_request_limit"]),
            scanner_min_book_depth=_int("SCANNER_MIN_BOOK_DEPTH", _DEFAULTS["scanner_min_book_depth"]),
            order_size=_float("ORDER_SIZE", _DEFAULTS["order_size"]),
            max_position_per_side=_float("MAX_POSITION_PER_SIDE", _DEFAULTS["max_position_per_side"]),
            max_open_orders=_int("MAX_OPEN_ORDERS", _DEFAULTS["max_open_orders"]),
            max_total_capital=_float("MAX_TOTAL_CAPITAL", _DEFAULTS["max_total_capital"]),
            sanity_test_price=_float("SANITY_TEST_PRICE", _DEFAULTS["sanity_test_price"]),
            sanity_test_size=_float("SANITY_TEST_SIZE", _DEFAULTS["sanity_test_size"]),
            movement_threshold=_float("MOVEMENT_THRESHOLD", _DEFAULTS["movement_threshold"]),
            yellow_snapshots=_int("YELLOW_SNAPSHOTS", _DEFAULTS["yellow_snapshots"]),
            inventory_threshold=_float("INVENTORY_THRESHOLD", _DEFAULTS["inventory_threshold"]),
            min_quote_spread=_float("MIN_QUOTE_SPREAD", _DEFAULTS["min_quote_spread"]),
            min_volume_24h=_float("MIN_VOLUME_24H", _DEFAULTS["min_volume_24h"]),
            max_volume_24h=_float("MAX_VOLUME_24H", _DEFAULTS["max_volume_24h"]),
            min_liquidity=_float("MIN_LIQUIDITY", _DEFAULTS["min_liquidity"]),
            inventory_refresh_seconds=_float("INVENTORY_REFRESH_SECONDS", _DEFAULTS["inventory_refresh_seconds"]),
            scan_retry_seconds=_float("SCAN_RETRY_SECONDS", _DEFAULTS["scan_retry_seconds"]),
            max_consecutive_errors=_int("MAX_CONSECUTIVE_ERRORS", _DEFAULTS["max_consecutive_errors"]),
            settlement_timeout_seconds=_float("SETTLEMENT_TIMEOUT_SECONDS", _DEFAULTS["settlement_timeout_seconds"]),
            stop_loss_distance=_float("STOP_LOSS_DISTANCE", _DEFAULTS["stop_loss_distance"]),
            min_pair_profit=_float("MIN_PAIR_PROFIT", _DEFAULTS["min_pair_profit"]),
        )
