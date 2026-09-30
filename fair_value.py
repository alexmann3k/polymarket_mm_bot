from __future__ import annotations

from models import OrderBook


def compute_fair_value(orderbook: OrderBook) -> float:
    """Pure v1 fair value: midpoint of best bid/ask."""
    if orderbook.best_bid is None or orderbook.best_ask is None:
        raise ValueError("Cannot compute fair value without both bid and ask.")
    return (orderbook.best_bid + orderbook.best_ask) / 2
