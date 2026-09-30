from __future__ import annotations

import logging
from typing import Any

from models import MarketCandidate, Position

logger = logging.getLogger(__name__)


def _cancel_and_confirm_orders(market: MarketCandidate, orders: Any) -> bool:
    """Cancel this market and wait until every cancellation is settled."""
    orders.cancel_market(market.condition_id)
    if not orders.finish_settlements():
        return False
    # A CLOB cancellation acknowledgement can precede the order-detail replica.
    # Keep this market active, but inactive for quoting, until every order from it
    # is terminal. This is an ordinary transient state, not a trading failure.
    if any(o.condition_id == market.condition_id for o in orders.open_orders.values()):
        logger.info("Market exit waiting for cancellation confirmation: %s", market.market_id)
        return False
    return True


def _reconcile_exit_position(
        market: MarketCandidate,
        orders: Any,
        inventory: Any,
        config: Any,
) -> Position | None:
    """Refresh exit balances and reject markets with inconsistent trade history."""
    inventory.reconcile_from_clob(
        orders.client, market.market_id, market.yes_token_id, market.no_token_id, config.signature_type)
    if {market.yes_token_id, market.no_token_id} & inventory.inconsistent_tokens:
        return None
    return inventory.positions[market.market_id]


def exit_market(
        market: MarketCandidate,
        orders: Any,
        inventory: Any,
        config: Any,
) -> bool:
    """Confirm quote cleanup and balances; caller retains non-neutral markets."""
    if not _cancel_and_confirm_orders(market, orders):
        return False
    position = _reconcile_exit_position(market, orders, inventory, config)
    if position is None:
        return False
    logger.info("Market deactivated %s; retained inventory YES=%.4f NO=%.4f",
                market.market_id, position.yes, position.no)
    return True
