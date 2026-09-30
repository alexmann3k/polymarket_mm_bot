from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any, Iterable

from models import Level, OrderBook


_PRICE_EPSILON = 1e-9
_SIZE_EPSILON = 1e-8


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from a mapping or response object."""
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _level_value(level: Any, name: str, short_name: str, default: Any = None) -> Any:
    """Read a level field while preserving mapping and SDK-object formats."""
    if isinstance(level, dict):
        return level.get(name, level.get(short_name, default))
    return getattr(level, name)


def _parse_level(level: Any) -> Level | None:
    price = float(_level_value(level, "price", "p", 0))
    size = float(_level_value(level, "size", "s", 0))
    if not math.isfinite(price) or not math.isfinite(size):
        raise ValueError("Non-finite orderbook level")
    return Level(price, size) if 0 < price < 1 and size > 0 else None


def _levels(raw: Any, *, reverse: bool) -> tuple[Level, ...]:
    """Parse, filter, and sort one side of a raw order book."""
    if raw is None:
        return ()
    out: list[Level] = []
    for level in raw:
        parsed = _parse_level(level)
        if parsed is not None:
            out.append(parsed)
    out.sort(key=lambda x: x.price, reverse=reverse)
    return tuple(out)


def get_orderbook(client: Any, token_id: str) -> OrderBook:
    """Fetch and normalize one token's order book."""
    return normalize_orderbook(client.get_order_book(token_id), token_id)


def _book_parameters(book: Any) -> tuple[float, float]:
    tick_size = float(_get(book, "tick_size", "tickSize"))
    min_order_size = float(
        _get(book, "min_order_size", "minimum_order_size", "minOrderSize")
    )
    if (not math.isfinite(tick_size) or not 0 < tick_size < 1
            or not math.isfinite(min_order_size) or min_order_size <= 0):
        raise ValueError("Invalid orderbook tick/minimum size")
    return tick_size, min_order_size


def normalize_orderbook(book: Any, token_id: str) -> OrderBook:
    """Validate and convert an exchange order book into the local model."""
    # Keep level parsing before metadata validation to preserve existing error
    # behavior for malformed level payloads.
    bids = _levels(_get(book, "bids"), reverse=True)
    tick_size, min_order_size = _book_parameters(book)
    asks = _levels(_get(book, "asks"), reverse=False)
    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        tick_size=tick_size,
        minimum_order_size=min_order_size,
        timestamp=time.time(),
    )


def get_pair_orderbooks(client: Any, yes_token_id: str, no_token_id: str) -> tuple[OrderBook, OrderBook]:
    """Fetch both outcome books and return them in YES/NO order."""
    raw = client.get_order_books([{"token_id": yes_token_id}, {"token_id": no_token_id}])
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("Expected both orderbooks in batch response")
    by_token = {str(_get(book, "asset_id")): book for book in raw}
    return tuple(normalize_orderbook(by_token[token], token) for token in (yes_token_id, no_token_id))


def _external_levels(
        book: OrderBook,
        levels: Iterable[Level],
        orders: Iterable[Any],
        side: str,
) -> tuple[Level, ...]:
    result: list[Level] = []
    for level in levels:
        own = sum(
            order.remaining
            for order in orders
            if order.token_id == book.token_id
            and order.side == side
            and abs(order.price - level.price) < _PRICE_EPSILON
        )
        if level.size - own > _SIZE_EPSILON:
            result.append(Level(level.price, level.size - own))
    return tuple(result)


def without_own_orders(book: OrderBook, orders: Iterable[Any]) -> OrderBook:
    """Subtract our remaining size while preserving other makers at each price."""
    return replace(
        book,
        bids=_external_levels(book, book.bids, orders, "BUY"),
        asks=_external_levels(book, book.asks, orders, "SELL"),
    )
