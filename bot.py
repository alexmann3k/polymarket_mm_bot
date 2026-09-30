from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from inventory import InventoryTracker
from market_lifecycle import exit_market
from models import MarketCandidate, OrderBook, Position
from orderbook import get_pair_orderbooks, without_own_orders
from orders import OrderManager
from risk import MarketHealth, RiskManager
from quoting import get_unmatched_position, should_trigger_stop_loss
from scanner import restore_inventory_markets, restore_token_ids

logger = logging.getLogger("main")

_STATUS_REPORT_INTERVAL_SECONDS = 30.0

_UnmatchedPosition = tuple[str, float, float | None]
_QuoteContext = tuple[
    Position,
    _UnmatchedPosition | None,
    bool,
    MarketHealth,
    tuple[OrderBook, OrderBook],
]


class Bot:
    """One trading thread; only the infrequent Gamma scan runs in a worker."""
    def __init__(self, config: Any, client: Any, inventory: InventoryTracker | None = None) -> None:
        self.config, self.client = config, client
        self.inventory = inventory if inventory is not None else InventoryTracker()
        self.orders = OrderManager(client, config)
        self.inventory.trade_history = self.orders.trade_history
        self.inventory.trade_owner = config.clob_api_key
        self.inventory.funder = config.funder
        self.risk = RiskManager(config)
        self.market = None
        self.health = None
        self.inventory_markets = {}
        self.inventory_health = {}
        self.inventory_quote_status = {}
        self.ranked_markets = deque()
        self.rejected = set()
        self.exit_reason = None
        self.last_inventory = 0.0
        self.startup_ready = False
        self.recovery_status = {}
        self.quarantined_markets = set()
        self.last_status = float("-inf")

    @property
    def active_market(self):
        # Keep the existing `market` interface for the runner and integrations.
        return self.market

    def initialize(self) -> None:
        self.inventory.load_local()
        self.orders.startup()

    def install_candidates(self, markets):
        # A full scheduled scan starts a new candidate generation. Don't jump away
        # from a healthy active market just because a different market ranks higher.
        self.rejected.clear()
        active = self.market.market_id if self.market else None
        self.ranked_markets = deque(m for m in markets
                                   if m.market_id != active and m.market_id not in self.inventory_markets)
        for m in markets:
            if m.market_id in self.inventory_markets:
                self.inventory_markets[m.market_id] = m
        if self.market:
            updated = next((m for m in markets if m.market_id == active), None)
            if updated:
                self.market = updated
            elif not self.exit_reason:
                self.exit_reason = "no_longer_scan_candidate"
                logger.warning("Leaving market %s: %s", active, self.exit_reason)

    def refresh_inventory(self) -> None:
        """Refresh only markets whose current balances can change a quote decision.

        Saved balanced/zero positions still reserve capital in RiskManager, but
        they have no live quote direction to manage and must not delay the loop.
        """
        self.orders.dirty_inventory = True
        tokens = {mid: self.inventory.tokens[mid] for mid in self.inventory_markets}
        if self.market:
            tokens[self.market.market_id] = (self.market.yes_token_id, self.market.no_token_id)
        try:
            if any(set(pair) & self.inventory.inconsistent_tokens for pair in tokens.values()):
                self.orders.refresh_trade_history()
                if self.orders.pending_trades:
                    self.orders.cancel_ids(self.orders.open_orders)
                    return
            collateral = (self.inventory.collateral_balance(self.client, self.config.signature_type)
                          if tokens else None)
            for mid, pair in tokens.items():
                self.inventory.reconcile_from_clob(self.client, mid, *pair, self.config.signature_type,
                                                   collateral)
        except Exception:
            self.orders.cancel_ids(self.orders.open_orders)
            raise
        self.orders.dirty_inventory = False
        self.last_inventory = time.monotonic()
        self.orders.cancel_inventory_forbidden(self.inventory)

    def retain_market(self, market, health=None):
        if market.market_id not in self.inventory_markets:
            self.inventory_markets[market.market_id] = market
            self.inventory_health[market.market_id] = health if health is not None else MarketHealth(self.config)
            self.inventory_quote_status[market.market_id] = None
            logger.info("Retaining market %s for inventory reduction", market.market_id)

    def move_active_to_recovery(self):
        """Transfer ownership without cancelling a still-valid reducing order."""
        market = self.market
        self.retain_market(market, self.health)
        self.market = self.health = None
        self.exit_reason = None
        logger.info("Market %s moved to recovery; normal market slot available (%d recovery markets)",
                    market.market_id, len(self.inventory_markets))

    def set_inventory_quote_status(self, market, status, reason):
        """Explain a retained market's state without logging every loop."""
        previous = self.inventory_quote_status.get(market.market_id)
        current = (status, reason)
        if previous != current:
            level = logging.INFO if status == "QUOTING" else logging.WARNING
            logger.log(level, "Inventory reduction %s market=%s: %s",
                       status.lower(), market.market_id, reason)
            self.inventory_quote_status[market.market_id] = current

    def finish_exit(self):
        if get_unmatched_position(self.inventory.positions[self.market.market_id]):
            self.move_active_to_recovery()
            return
        if exit_market(self.market, self.orders, self.inventory, self.config):
            position = self.inventory.positions[self.market.market_id]
            if get_unmatched_position(position):
                self.move_active_to_recovery()
                return  # Cancellation raced a fill: retain ownership and recover.
            self.rejected.add(self.market.market_id)
            self.market = self.health = None
            self.exit_reason = None
            self.prune_empty_saved_markets()

    def prune_empty_saved_markets(self):
        protected = set(self.inventory_markets)
        if self.market:
            protected.add(self.market.market_id)
        return self.inventory.prune_empty_markets(self.orders, protected)

    def _settlement_pending(self) -> bool:
        """Return whether fills or settlement work still make balances uncertain."""
        return bool(self.orders.finalizing or self.orders.pending_trades)

    def _inventory_refresh_required(self) -> bool:
        return (self.orders.dirty_inventory
                or time.monotonic() - self.last_inventory >= self.config.inventory_refresh_seconds)

    def _quote_replacement_allowed(self) -> bool:
        """Allow replacement only after inventory and settlement state is clean."""
        return not (self.orders.dirty_inventory or self._settlement_pending())

    def orders_unsettled(self) -> bool:
        return bool(self.orders.dirty_inventory or self._settlement_pending())

    def report_status(self):
        now = time.monotonic()
        if now - self.last_status < _STATUS_REPORT_INTERVAL_SECONDS:
            return
        self.last_status = now
        logger.info("Trading status: active=%s open_orders=%d recovery=%d quarantined=%s "
                    "candidates=%d startup_ready=%s dirty_inventory=%s finalizing=%d pending_trades=%d",
                    self.market.market_id if self.market else "none", len(self.orders.open_orders),
                    len(self.inventory_markets), ",".join(sorted(self.quarantined_markets)) or "none",
                    len(self.ranked_markets), self.startup_ready, self.orders.dirty_inventory,
                    len(self.orders.finalizing), len(self.orders.pending_trades))

    def _handle_startup(self) -> bool:
        """Reconcile persisted state before allowing a normal trading cycle."""
        if self.startup_ready:
            return False
        if not self.orders.finish_settlements():
            return True
        missing = [mid for mid, p in self.inventory.positions.items()
                   if mid not in self.inventory.tokens]
        if missing:
            self.inventory.tokens.update(restore_token_ids(self.config, missing))
        # Start with current CLOB balances rather than trusting the old CSV snapshot.
        self.inventory.reconcile_saved_positions(self.client, self.config.signature_type)
        # Balances may have changed while offline; retain only currently held markets.
        held = {mid for mid, p in self.inventory.positions.items() if get_unmatched_position(p)}
        known = {m.market_id: m for m in self.ranked_markets}
        known.update(self.inventory_markets)
        if self.market:
            known[self.market.market_id] = self.market
        known.update(restore_inventory_markets(self.config, held - known.keys()))
        for mid in held:
            if not self.market or mid != self.market.market_id:
                self.retain_market(known[mid])
        # Startup reconciled every net position, so do not repeat those calls this cycle.
        self.orders.dirty_inventory = False
        self.last_inventory = time.monotonic()
        self.orders.cancel_inventory_forbidden(self.inventory)
        self.startup_ready = True
        return False

    def _synchronize_orders(self) -> bool:
        if not self.orders.sync():
            return True
        if self._settlement_pending():
            # Balances may not yet include fills, so keep the settlement quarantine.
            self.orders.cancel_ids(self.orders.open_orders)
            if self.exit_reason:
                self.finish_exit()
            return True
        return False

    def _refresh_inventory_phase(self) -> bool:
        if self._inventory_refresh_required():
            self.refresh_inventory()
        return self.orders_unsettled()

    def _quarantine_mismatches(self) -> bool:
        managed = dict(self.inventory_markets)
        if self.market:
            managed[self.market.market_id] = self.market
        mismatches = {mid for mid, m in managed.items()
                      if {m.yes_token_id, m.no_token_id} & self.inventory.inconsistent_tokens}
        for mid in sorted(mismatches - self.quarantined_markets):
            logger.warning("Trade/balance reconciliation PAUSED market=%s: history disagrees with "
                           "balances; quarantining this market and refreshing history", mid)
        for mid in sorted(self.quarantined_markets - mismatches):
            logger.info("Trade/balance reconciliation RESUMED market=%s", mid)
        self.quarantined_markets = mismatches
        for mid in mismatches:
            self.orders.cancel_market(managed[mid].condition_id)
        # Cancellation/fill uncertainty still blocks the account until settled.
        if self.orders_unsettled():
            return True
        if self.market and self.market.market_id in mismatches:
            self.retain_market(self.market, self.health)
            self.market = self.health = None
            self.exit_reason = None
        return False

    def _process_recovery_markets(self) -> bool:
        if self.market and (get_unmatched_position(self.inventory.positions[self.market.market_id])
                            or self.market.market_id in self.inventory.stops):
            self.move_active_to_recovery()

        for mid, market in list(self.inventory_markets.items()):
            if mid in self.quarantined_markets:
                self.set_inventory_quote_status(market, "PAUSED", "trade/balance mismatch; market quarantined")
                continue
            position = self.inventory.positions[mid]
            if get_unmatched_position(position) is None:
                if exit_market(market, self.orders, self.inventory, self.config):
                    position = self.inventory.positions[mid]
                    if get_unmatched_position(position) is None:
                        self.complete_recovery(market)
                        del self.inventory_markets[mid]
                        del self.inventory_health[mid]
                        self.inventory_quote_status.pop(mid, None)
                        logger.info("Inventory management complete market=%s: neutral, orders cleared", mid)
                        self.prune_empty_saved_markets()
            else:
                self.quote_market(market, self.inventory_health[mid], reduction_only=True)
            if self.orders_unsettled():
                return True
        return False

    def _handle_active_exit(self) -> bool:
        if self.exit_reason:
            self.finish_exit()
            return True
        return False

    def _select_active_market(self) -> bool:
        if self.market is not None:
            return False
        # One fresh candidate check per loop: no burst through the entire list.
        while self.ranked_markets and (self.ranked_markets[0].market_id in self.rejected
                                      or self.ranked_markets[0].market_id in self.inventory_markets):
            self.ranked_markets.popleft()
        if not self.ranked_markets:
            return True
        candidate = self.ranked_markets[0]  # Retain it if the request fails.
        books = get_pair_orderbooks(self.client, candidate.yes_token_id, candidate.no_token_id)
        health = MarketHealth(self.config)
        state, reason = health.update(books, candidate.end_date)
        self.ranked_markets.popleft()
        if state == "RED":
            logger.info("Candidate skipped %s: %s", candidate.market_id, reason)
            self.rejected.add(candidate.market_id)
            return True
        self.market, self.health = candidate, health
        self.orders.dirty_inventory = True
        logger.info("Selected market %s score=%.3f %s", candidate.market_id, candidate.score, candidate.question)
        # Wait for a second snapshot before first quoting, so movement is measurable.
        self.refresh_inventory()
        return True

    def _run_order_safety_watchdog(self) -> None:
        """Expire stale orders before making account or market decisions."""
        self.orders.cancel_expired()

    def _reconcile_account_state(self) -> bool:
        """Run account gates in order; return when the cycle must stop safely."""
        if self._handle_startup():
            return True
        if self._synchronize_orders():
            return True
        if self._refresh_inventory_phase():
            return True
        self.prune_empty_saved_markets()
        return self._quarantine_mismatches()

    def _manage_market_lifecycle(self) -> bool:
        """Process recovery, active-market exit, and candidate selection in order."""
        if self._process_recovery_markets():
            return True
        if self._handle_active_exit():
            return True
        return self._select_active_market()

    def _quote_active_market(self) -> None:
        self.quote_market(self.market, self.health)

    def step(self) -> None:
        self._run_order_safety_watchdog()
        if self._reconcile_account_state():
            return
        if self._manage_market_lifecycle():
            return
        self._quote_active_market()

    def complete_recovery(self, market):
        mid = market.market_id
        self.orders.dry_stops.discard(mid)
        stopped = self.inventory.stops.pop(mid, None)
        if stopped:
            self.inventory.save()
        if self.recovery_status.pop(mid, None) or stopped:
            logger.info("%s market=%s", "POSITION CLOSED/PAIRED AFTER STOP LOSS" if stopped else "POSITION PAIRED", mid)

    def manage_stop_loss(self, market, books, position):
        mid = market.market_id
        unmatched = get_unmatched_position(position)
        stopped = self.inventory.stops.get(mid)
        if unmatched is None:
            if stopped:
                self.orders.cancel_market(market.condition_id)
                if not self.orders_unsettled():
                    self.complete_recovery(market)
                return True
            return False
        side, qty, entry = unmatched
        if stopped and stopped != side:
            # An external trade/cancel race hedged the original side. Never reverse-sell.
            self.orders.cancel_market(market.condition_id)
            if not self.orders_unsettled():
                self.inventory.stops.pop(mid)
                self.inventory.save()
            return True
        if not stopped and not should_trigger_stop_loss(position, books, self.config.stop_loss_distance):
            return False
        if stopped and entry is None:
            self.orders.cancel_market(market.condition_id)
            self.set_inventory_quote_status(market, "STOP", "waiting for reconciled entry/quantity before retry")
            return True
        if not stopped:
            logger.warning("STOP LOSS TRIGGERED market=%s %s qty=%.6f entry=%.4f best_bid=%.4f threshold=%.4f",
                           mid, side, qty, entry, books[0 if side == "YES" else 1].best_bid,
                           entry - self.config.stop_loss_distance)
            self.inventory.stops[mid] = side
            try:
                self.inventory.save()  # Persist intent before any execution, including restart.
            except Exception:
                self.inventory.stops.pop(mid, None)
                raise
            self.orders.dirty_inventory = True
        had_orders = any(o.condition_id == market.condition_id for o in self.orders.open_orders.values())
        if had_orders:
            self.orders.cancel_market(market.condition_id)
            self.orders.dirty_inventory = True  # Fresh balances after cancellation, even without observed fills.
        if self.orders_unsettled():
            return True
        held_book = books[0 if side == "YES" else 1]
        placed = self.orders.close_unmatched_position(market, held_book, qty)
        self.set_inventory_quote_status(market, "STOP", "FAK close submitted" if placed else "waiting: no valid executable close (size, price, liquidity or order limit)")
        return True

    def _prepare_quote_context(
            self,
            market: MarketCandidate,
            health: MarketHealth,
            reduction_only: bool,
    ) -> _QuoteContext | None:
        position = self.inventory.positions[market.market_id]
        unmatched = get_unmatched_position(position)
        reduction_only = reduction_only or unmatched is not None
        if unmatched and self.recovery_status.get(market.market_id) != unmatched:
            previous = self.recovery_status.get(market.market_id)
            logger.info("%s market=%s %s qty=%.6f avg_entry=%s",
                        "RECOVERY PARTIAL FILL / POSITION UPDATE" if previous else "UNPAIRED POSITION",
                        market.market_id, *unmatched)
            self.recovery_status[market.market_id] = unmatched
        if reduction_only and (market.raw.get("closed") is True
                               or market.raw.get("acceptingOrders") is False
                               or market.raw.get("enableOrderBook") is False):
            # Resolved books may no longer exist. Keep holdings without letting a
            # predictable book 404 stop management of other, tradable markets.
            self.orders.cancel_market(market.condition_id)
            self.set_inventory_quote_status(market, "PAUSED", "market closed or orders disabled")
            return None
        if reduction_only and health.state == "RED":
            # RED still ends that health session. A retained market must establish
            # two new healthy snapshots before reduction quoting may restart.
            health = MarketHealth(self.config)
            if self.market and market.market_id == self.market.market_id:
                self.health = health
            else:
                self.inventory_health[market.market_id] = health
        raw_books = get_pair_orderbooks(self.client, market.yes_token_id, market.no_token_id)
        own = () if self.config.dry_run else tuple(self.orders.open_orders.values())
        books = tuple(without_own_orders(b, own) for b in raw_books)
        return position, unmatched, reduction_only, health, books

    def _handle_market_health(
            self,
            market: MarketCandidate,
            health: MarketHealth,
            books: tuple[OrderBook, OrderBook],
            unmatched: _UnmatchedPosition | None,
            reduction_only: bool,
    ) -> bool:
        """Apply health gating and return whether this quote cycle is complete."""
        state, reason = health.update(books, market.end_date, inventory_reduction=unmatched is not None)
        if state == "RED":
            if reduction_only:
                self.orders.cancel_market(market.condition_id)
                self.set_inventory_quote_status(market, "PAUSED", reason)
                return True
            logger.warning("Leaving market %s: %s", market.market_id, reason)
            self.exit_reason = reason
            # Cancel immediately in this iteration; switch only after confirmed cleanup.
            self.finish_exit()
            return True
        if state == "YELLOW":
            self.orders.cancel_market(market.condition_id)
            if reduction_only:
                self.set_inventory_quote_status(market, "PAUSED", reason)
            return True
        if reduction_only and len(health.history) < 2:
            return True
        return False

    def _apply_quote_targets(
            self,
            market: MarketCandidate,
            books: tuple[OrderBook, OrderBook],
            unmatched: _UnmatchedPosition | None,
            reduction_only: bool,
    ) -> None:
        self.orders.last_healthy = time.monotonic()
        position = self.inventory.positions[market.market_id]
        targets = self.risk.targets(market, books, position, self.inventory,
                                    tuple(self.orders.open_orders.values()))
        if reduction_only:
            self.set_inventory_quote_status(
                market, "QUOTING" if targets else "PAUSED",
                ("RECOVERY MODE: " + str(targets)) if targets else
                "unknown entry cost" if unmatched and unmatched[2] is None else
                "risk limits, price cap or unmatched quantity below minimum order size")
        if self.orders.refresh_quotes(market.condition_id, inventory_reduction=reduction_only):
            # Confirm cancellation and final fills before any replacement.
            if self._quote_replacement_allowed():
                targets = self.risk.targets(market, books, position, self.inventory,
                                            tuple(self.orders.open_orders.values()))
                self.orders.place_missing(market, targets, inventory_reduction=reduction_only)
            return
        if self.orders.cancel_obsolete(targets, market.condition_id):
            # Cancellation can race a fill; next cycle gets new balances and books.
            return
        self.orders.place_missing(market, targets, inventory_reduction=reduction_only)

    def quote_market(
            self,
            market: MarketCandidate,
            health: MarketHealth,
            reduction_only: bool = False,
    ) -> None:
        context = self._prepare_quote_context(market, health, reduction_only)
        if context is None:
            return
        position, unmatched, reduction_only, health, books = context
        if self.manage_stop_loss(market, books, position):
            return
        if self._handle_market_health(market, health, books, unmatched, reduction_only):
            return
        self._apply_quote_targets(market, books, unmatched, reduction_only)
