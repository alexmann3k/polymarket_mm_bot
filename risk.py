from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR
from typing import Any, Sequence

from models import OrderBook, Position
from quoting import (
    get_unmatched_position,
    has_narrow_spread,
    inventory_allows_order,
    quote_book,
    recovery_price,
    round_to_tick,
)

logger = logging.getLogger(__name__)

_PRICE_EPSILON = 1e-9
_SPREAD_EPSILON = 1e-9
_MOVEMENT_EPSILON = 1e-9
_ACCOUNT_EPSILON = 1e-9
QuoteTarget = tuple[float, float] | tuple[float, float, float]


@dataclass(frozen=True)
class Snapshot:
    best_bid: float
    best_ask: float
    mid: float
    spread: float


@dataclass
class _ReservationState:
    capital: float
    cash: float
    slots: int
    existing: dict[tuple[str, str], Any]


def _book_rejection_reason(book: OrderBook, config: Any, *, narrow_recovery: bool) -> str | None:
    """Return the first rejection reason for a book with both sides present."""
    if book.spread < config.min_spread - _SPREAD_EPSILON and not narrow_recovery:
        return "spread_collapse"
    if (book.spread > config.max_spread + _SPREAD_EPSILON
            or not config.min_price <= book.mid <= config.max_price):
        return "price_or_spread_limit"
    if min(len(book.bids), len(book.asks)) < config.scanner_min_book_depth:
        return "insufficient_depth"
    if book.minimum_order_size > config.order_size:
        return "minimum_order_size"
    if quote_book(book, config) is None and not narrow_recovery:
        return "no_edge_after_ticks"
    return None


def _price_movement(current: Sequence[Snapshot], previous: Sequence[Snapshot]) -> float:
    return max(
        max(abs(current_book.best_bid - previous_book.best_bid),
            abs(current_book.best_ask - previous_book.best_ask))
        for current_book, previous_book in zip(current, previous)
    )


class MarketHealth:
    """Track whether a two-token market is safe to quote; performs no I/O."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.history = deque(maxlen=config.yellow_snapshots + 1)
        self.state = "GREEN"
        self.reason = "normal"
        self.observed = 0
        self.stable = 0

    def resolution_cutoff(self, end_date: datetime) -> bool:
        return (end_date - datetime.now(timezone.utc)).total_seconds() <= self.config.min_resolution_hours * 3600

    def _healthy_snapshot(self, snapshots: list[Snapshot]) -> float:
        movement = _price_movement(snapshots, self.history[-1]) if self.history else 0.0
        self.history.append(tuple(snapshots))
        jump = movement > self.config.movement_threshold + _MOVEMENT_EPSILON

        if self.state == "GREEN" and jump:
            self.state, self.reason = "YELLOW", "sudden_movement"
            self.observed = self.stable = 0
        elif self.state == "YELLOW":
            self.observed += 1
            self.stable = 0 if jump else self.stable + 1
            if self.stable >= self.config.yellow_snapshots:
                self.state, self.reason = "GREEN", "stabilized"
            elif self.observed >= self.config.yellow_snapshots:
                self.state, self.reason = "RED", "persistent_movement"
        return movement

    def update(
            self,
            books: Sequence[OrderBook],
            end_date: datetime,
            *,
            inventory_reduction: bool = False,
    ) -> tuple[str, str]:
        """Validate books and update the GREEN/YELLOW/RED health state."""
        if self.state == "RED":
            return self.state, self.reason

        reason = "resolution_cutoff" if self.resolution_cutoff(end_date) else None
        snapshots: list[Snapshot] = []
        for book in books:
            if not book.bids or not book.asks:
                reason = reason or "empty_orderbook"
                continue
            snapshots.append(Snapshot(book.best_bid, book.best_ask, book.mid, book.spread))
            narrow_recovery = inventory_reduction and has_narrow_spread(book, self.config)
            book_reason = _book_rejection_reason(
                book,
                self.config,
                narrow_recovery=narrow_recovery,
            )
            reason = reason or book_reason

        old = self.state
        movement = 0.0
        if reason:
            self.state, self.reason = "RED", reason
        else:
            movement = self._healthy_snapshot(snapshots)
        if old != self.state:
            logger.warning("Health %s -> %s: %s move=%.4f", old, self.state, self.reason, movement)
        return self.state, self.reason


def _reservation_state(
        market: Any,
        position: Position,
        inventory: Any,
        open_orders: Sequence[Any],
) -> _ReservationState:
    """Calculate capital, cash, slots, and existing orders outside this market."""
    capital = sum(p.yes + p.no for p in inventory.positions.values())
    cash = position.usdc
    tokens = {market.yes_token_id, market.no_token_id}
    other_orders = [order for order in open_orders if order.token_id not in tokens]
    capital += sum(order.remaining for order in other_orders if order.side == "BUY")
    cash -= sum(order.remaining * order.price for order in other_orders if order.side == "BUY")
    slots = len(other_orders)
    existing = {(order.token_id, order.side): order for order in open_orders}
    return _ReservationState(capital, cash, slots, existing)


def _candidate_prices(
        book: OrderBook,
        existing: dict[tuple[str, str], Any],
        keep_narrow_recovery: bool,
        config: Any,
) -> tuple[tuple[float, float], Any] | None:
    """Build normal prices, or reuse a resting recovery price when needed."""
    prices = quote_book(book, config)
    resting = existing.get((book.token_id, "BUY")) if keep_narrow_recovery else None
    if prices is not None:
        return prices, resting
    if not resting:
        return None
    # A narrow book may have no new two-sided quote, but a valid existing
    # recovery BUY can remain eligible for the inventory-reduction path.
    return (resting.price, resting.price), resting


def _recovery_candidate(
        book: OrderBook,
        old_order: Any,
        resting_recovery: Any,
        unmatched: tuple[str, float, float | None],
        config: Any,
) -> tuple[float, float] | None:
    """Build and size the inventory-reduction candidate for one book."""
    price = recovery_price(
        book,
        unmatched[2],
        config,
        resting_price=resting_recovery.price if resting_recovery else None,
    )
    if price is None:
        return None

    size = round_to_tick(min(config.order_size, unmatched[1]), .01, ROUND_FLOOR)
    keep = bool(old_order and abs(old_order.price - price) < _PRICE_EPSILON
                and old_order.remaining <= size + _ACCOUNT_EPSILON)
    if keep:
        size = old_order.remaining
    if size <= 0 or (not keep and size < book.minimum_order_size):
        return None
    return price, size


def _build_order_candidate(
        book: OrderBook,
        proposed_price: float,
        old_order: Any,
        resting_recovery: Any,
        unmatched: tuple[str, float, float | None] | None,
        config: Any,
) -> tuple[float, float] | None:
    """Build a normal or recovery candidate without applying account limits."""
    size = (old_order.remaining
            if old_order and abs(old_order.price - proposed_price) < _PRICE_EPSILON
            else config.order_size)
    if unmatched is None:
        return proposed_price, size
    return _recovery_candidate(book, old_order, resting_recovery, unmatched, config)


def _passes_account_limits(
        side: str,
        held: float,
        size: float,
        price: float,
        capital: float,
        cash: float,
        config: Any,
) -> bool:
    if side == "SELL":
        if held + _ACCOUNT_EPSILON < size:
            return False
        return True
    if held + size > config.max_position_per_side + _ACCOUNT_EPSILON:
        return False
    return not (
        capital + size > config.max_total_capital + _ACCOUNT_EPSILON
        or cash < size * price
    )


def _target_value(
        price: float,
        tick_size: float,
        size: float,
        unmatched: tuple[str, float, float | None] | None,
) -> QuoteTarget:
    """Keep the existing two-field/three-field target protocol intact."""
    return (price, tick_size, size) if unmatched else (price, tick_size)


class RiskManager:
    """Build quote targets that satisfy inventory, capital, and order limits."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def targets(
            self,
            market: Any,
            books: Sequence[OrderBook],
            position: Position,
            inventory: Any,
            open_orders: Sequence[Any],
    ) -> dict[tuple[str, str], QuoteTarget]:
        """Return desired quotes after applying all account and position limits."""
        reservations = _reservation_state(
            market, position, inventory, open_orders)
        result: dict[tuple[str, str], QuoteTarget] = {}
        unmatched = get_unmatched_position(position)
        keep_narrow_recovery = (
            unmatched is not None
            and any(has_narrow_spread(book, self.config) for book in books)
        )

        for book, held, outcome in (
                (books[0], position.yes, "YES"),
                (books[1], position.no, "NO"),
        ):
            candidate_prices = _candidate_prices(
                book,
                reservations.existing,
                keep_narrow_recovery,
                self.config,
            )
            if candidate_prices is None:
                continue
            prices, resting = candidate_prices

            bid, ask = prices
            for side, proposed_price in (("SELL", ask), ("BUY", bid)):
                if not inventory_allows_order(position.yes, position.no, outcome, side):
                    continue
                old_order = reservations.existing.get((book.token_id, side))
                target = _build_order_candidate(
                    book,
                    proposed_price,
                    old_order,
                    resting,
                    unmatched,
                    self.config,
                )
                if target is None or reservations.slots >= self.config.max_open_orders:
                    continue
                price, size = target
                if not _passes_account_limits(
                        side, held, size, price,
                        reservations.capital, reservations.cash, self.config):
                    continue

                key = (book.token_id, side)
                result[key] = _target_value(price, book.tick_size, size, unmatched)
                reservations.slots += 1
                if side == "BUY":
                    reservations.capital += size
                    reservations.cash -= size * price
        return result
