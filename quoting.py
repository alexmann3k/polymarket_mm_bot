from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from typing import Any, Sequence

from models import OrderBook, Position


# At most one cent-share (<= $0.01 payout), the SDK's size precision.
# This ignores unexecutable dust, not the exchange's much larger order minimum.
INVENTORY_EPSILON = 0.01
_SPREAD_EPSILON = 1e-9
_STOP_LOSS_EPSILON = 1e-12


def get_unmatched_position(
        position: Position,
) -> tuple[str, float, float | None] | None:
    """Return the unmatched outcome, quantity, and entry cost, if any."""
    mode = inventory_mode(position.yes, position.no)
    if mode == "BALANCED":
        return None
    if mode == "LONG_YES":
        return "YES", position.yes - position.no, position.avg_entry_yes
    return "NO", position.no - position.yes, position.avg_entry_no


def should_trigger_stop_loss(
        position: Position,
        books: Sequence[OrderBook],
        distance: float,
) -> bool:
    """Return whether the unmatched position is at or below its stop price."""
    unmatched = get_unmatched_position(position)
    if unmatched is None or unmatched[2] is None:
        return False
    side, _, entry = unmatched
    bid = books[0 if side == "YES" else 1].best_bid
    threshold = entry - distance
    return threshold > 0 and bid is not None and bid <= threshold + _STOP_LOSS_EPSILON


def _pair_price_cap(entry: float, config: Any) -> Decimal:
    return Decimal("1") - Decimal(str(entry)) - Decimal(str(config.min_pair_profit))


def _buy_price_bounds(book: OrderBook, config: Any) -> tuple[float, float]:
    return max(config.min_price, book.tick_size), min(config.max_price, 1 - book.tick_size)


def _valid_resting_recovery_price(
        book: OrderBook,
        config: Any,
        cap: Decimal,
        resting_price: float,
) -> bool:
    if book.best_ask is None:
        return False
    lower, upper = _buy_price_bounds(book, config)
    price = Decimal(str(resting_price))
    return (
        price <= cap
        and lower <= resting_price <= upper
        and resting_price < book.best_ask
        and round_to_tick(resting_price, book.tick_size) == resting_price
    )


def recovery_price(
        book: OrderBook,
        entry: float | None,
        config: Any,
        *,
        resting_price: float | None = None,
) -> float | None:
    """Calculate a non-crossing recovery BUY price within the pair cap."""
    if entry is None:
        return None
    cap = _pair_price_cap(entry, config)

    if resting_price is not None:
        # A narrow spread may prevent a new two-sided quote while an existing
        # recovery BUY is still valid. Keep its price, but recheck all BUY bounds.
        return (resting_price
                if _valid_resting_recovery_price(book, config, cap, resting_price)
                else None)

    prices = quote_book(book, config)
    if prices is None:
        return None
    price = round_to_tick(
        min(Decimal(str(prices[0])), cap),
        book.tick_size,
        ROUND_FLOOR,
    )
    lower, _ = _buy_price_bounds(book, config)
    return price if lower <= price <= config.max_price else None


def has_narrow_spread(book: OrderBook, config: Any) -> bool:
    """Return whether the spread is positive but below the normal minimum."""
    return book.spread is not None and 0 < book.spread < config.min_spread - _SPREAD_EPSILON


def inventory_mode(yes_position: float, no_position: float) -> str:
    """Classify the net inventory after ignoring negligible share dust."""
    net = yes_position - no_position
    if net > INVENTORY_EPSILON:
        return "LONG_YES"
    if net < -INVENTORY_EPSILON:
        return "LONG_NO"
    return "BALANCED"


def get_allowed_quote_sides(yes_position: float, no_position: float) -> dict[str, bool]:
    """Return which outcomes may receive BUY quotes for the current inventory."""
    mode = inventory_mode(yes_position, no_position)
    return {"YES": mode != "LONG_YES", "NO": mode != "LONG_NO"}


def inventory_allows_order(
        yes_position: float,
        no_position: float,
        outcome: str,
        side: str,
) -> bool:
    """Allow only inventory-reducing BUYs while an outcome is imbalanced."""
    allowed = get_allowed_quote_sides(yes_position, no_position)
    if inventory_mode(yes_position, no_position) == "BALANCED":
        return allowed[outcome]
    return side == "BUY" and allowed[outcome]


def round_to_tick(
        price: float | Decimal,
        tick_size: float | Decimal,
        rounding: str = ROUND_HALF_EVEN,
) -> float:
    """Round a price to a positive, finite tick size using Decimal arithmetic."""
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))
    if (not price_decimal.is_finite()
            or not tick_decimal.is_finite()
            or tick_decimal <= 0):
        raise ValueError("Price and tick must be finite; tick must be positive")
    return float(
        (price_decimal / tick_decimal).to_integral_value(rounding=rounding)
        * tick_decimal
    )


def _round_decimal_to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding=rounding) * tick


def _quote_is_valid(
        book: OrderBook,
        config: Any,
        bid: Decimal,
        ask: Decimal,
        buy: Decimal,
        sell: Decimal,
) -> bool:
    min_price = Decimal(str(config.min_price))
    max_price = Decimal(str(config.max_price))
    min_quote_spread = Decimal(str(config.min_quote_spread))
    tick = Decimal(str(book.tick_size))
    if not min_price <= buy < sell <= max_price:
        return False
    if buy >= ask or sell <= bid:
        return False
    if sell - buy < min_quote_spread:
        return False
    if buy < tick or sell > Decimal("1") - tick:
        return False
    return book.minimum_order_size <= config.order_size


def quote_book(book: OrderBook, config: Any) -> tuple[float, float] | None:
    """Improve the external BBO by one tick without crossing or erasing the edge."""
    if book.best_bid is None or book.best_ask is None:
        return None

    bid = Decimal(str(book.best_bid))
    ask = Decimal(str(book.best_ask))
    tick = Decimal(str(book.tick_size))
    mid = (bid + ask) / 2
    buy = _round_decimal_to_tick(bid + tick, tick, ROUND_FLOOR)
    sell = _round_decimal_to_tick(max(ask - tick, mid), tick, ROUND_CEILING)
    if not _quote_is_valid(book, config, bid, ask, buy, sell):
        return None
    return float(buy), float(sell)
