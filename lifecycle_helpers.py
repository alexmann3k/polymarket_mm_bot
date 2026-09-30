from __future__ import annotations

from models import Position


def safe_position(inventory, market_id: str) -> Position:
    return inventory.positions.get(market_id, Position())
