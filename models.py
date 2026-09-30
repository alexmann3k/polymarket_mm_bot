from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: float
    minimum_order_size: float
    timestamp: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        return (self.best_bid + self.best_ask) / 2 if self.bids and self.asks else None

    @property
    def spread(self) -> float | None:
        return self.best_ask - self.best_bid if self.bids and self.asks else None


@dataclass(frozen=True)
class MarketCandidate:
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    end_date: datetime
    volume: float
    spread: float
    category: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Position:
    yes: float = 0.0
    no: float = 0.0
    usdc: float = 0.0
    avg_entry_yes: float | None = None
    avg_entry_no: float | None = None
