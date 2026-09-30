from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TypeAlias
from uuid import uuid4

from quoting import inventory_allows_order

from py_clob_client_v2 import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersV2Args,
    Side,
    TradeParams,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 5.0
_FILL_EPSILON = 1e-8
_PRICE_EPSILON = 1e-9
_SIZE_EPSILON = 1e-9

_OrderKey: TypeAlias = tuple[str, str]
_NormalTarget: TypeAlias = tuple[float, float]
_RecoveryTarget: TypeAlias = tuple[float, float, float]
_Target: TypeAlias = _NormalTarget | _RecoveryTarget


class OrderStateError(RuntimeError):
    """Uncertain write or unexpected account activity: stop placing orders."""


@dataclass
class TrackedOrder:
    """Local state for an order whose exchange status is being reconciled."""

    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    created_at: float
    condition_id: str = ""
    matched: float = 0.0
    expiration: int = 0

    @property
    def remaining(self):
        return max(0.0, self.size - self.matched)


class OrderManager:
    """Track exchange orders and prevent writes while account state is uncertain."""

    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.open_orders: dict[str, TrackedOrder] = {}
        # During cancel/fill reconciliation the same object intentionally remains
        # in open_orders while finalizing quarantines it until terminal detail arrives.
        self.finalizing: dict[str, TrackedOrder] = {}
        self.cancel_acknowledged: set[str] = set()
        self.recently_finalized: dict[str, float] = {}
        self.pending_trades: set[str] = set()
        self.trade_history = {}
        self.dry_stops = set()
        self.dirty_inventory = True
        self.last_healthy = time.monotonic()
        self.pending_since = None
        self.blocked = False
        self.heartbeat_id = ""
        self.last_heartbeat = float("-inf")

    def keepalive(self):
        """Renew only from a healthy trading cycle, never from an independent thread."""
        if (self.config.dry_run
                or time.monotonic() - self.last_heartbeat < _HEARTBEAT_INTERVAL_SECONDS):
            return
        try:
            response = self.client.post_heartbeat(self.heartbeat_id)
        except Exception as exc:
            error = getattr(exc, "error_msg", None)
            if isinstance(error, dict) and error.get("heartbeat_id"):
                self.heartbeat_id = error["heartbeat_id"]
            raise
        if not isinstance(response, dict) or not response.get("heartbeat_id"):
            raise RuntimeError("Heartbeat not confirmed")
        self.heartbeat_id = response["heartbeat_id"]
        self.last_heartbeat = time.monotonic()

    def startup(self):
        """A dedicated bot account is required. Recover orders from previous runs."""
        if self.config.dry_run:
            return
        # Account-wide cancellation also covers a lost placement response before a crash.
        self.cancel_all()
        # Once on startup: include old, still settling fills, not only the first page.
        self.refresh_trade_history()

    def refresh_trade_history(self):
        if self.config.dry_run:
            return
        rows = self.client.get_trades()
        if not isinstance(rows, list) or any(not isinstance(t, dict) or not t.get("id") for t in rows):
            raise ValueError("Invalid trade history response")
        for trade in rows:
            self.trade_history[str(trade["id"])] = trade
            if str(trade.get("status", "")).removeprefix("TRADE_STATUS_") not in ("CONFIRMED", "FAILED"):
                self.pending_trades.add(str(trade["id"]))

    def _observe(self, tracked, row):
        response_id = None
        if isinstance(row, dict):
            response_id = row.get("id") or row.get("orderID") or row.get("order_id")
        if str(response_id) != tracked.order_id:
            raise ValueError("Invalid order response")
        original, matched = float(row["original_size"]), float(row["size_matched"])
        if not all(math.isfinite(x) for x in (original, matched)) or not 0 <= matched <= original or original <= 0:
            raise ValueError("Invalid order sizes")
        # Ratio handles both decimalized and fixed-6 REST responses without guessing units.
        filled = tracked.size * matched / original
        if filled + _FILL_EPSILON < tracked.matched:
            raise RuntimeError("Order fill state moved backwards; wait for consistent response")
        if filled > tracked.matched + _FILL_EPSILON:
            logger.info("Order filled id=%s side=%s delta=%.4f total=%.4f (settlement pending)",
                        tracked.order_id, tracked.side, filled - tracked.matched, filled)
            tracked.matched = filled
            self.dirty_inventory = True
            trades = row.get("associate_trades")
            if not trades:
                # Do not permit replacement until the matching trade IDs become visible.
                self.finalizing[tracked.order_id] = tracked
            else:
                self.pending_trades.update(str(t) for t in trades)

    def sync(self):
        """One account-wide open-order request; extra detail only for changed orders."""
        if self.config.dry_run:
            for oid, order in list(self.open_orders.items()):
                if order.expiration and time.time() >= order.expiration:
                    del self.open_orders[oid]
            return True
        rows = self.client.get_open_orders()
        if not isinstance(rows, list):
            raise ValueError("Invalid open-orders response")
        remote = {str(row["id"]): row for row in rows}
        now = time.monotonic()
        for oid in list(self.recently_finalized):
            if oid not in remote:
                del self.recently_finalized[oid]
        stale_remote = set(remote) & set(self.recently_finalized)
        unknown = set(remote) - set(self.open_orders) - stale_remote
        if unknown:
            self.blocked = True
            raise OrderStateError("Unexpected open order; use a dedicated account, recovery required")
        if stale_remote:
            if any(now - self.recently_finalized[oid] > self.config.settlement_timeout_seconds
                   for oid in stale_remote):
                raise OrderStateError("Open-order list remained stale beyond settlement timeout")
            logger.warning("Open-order list still shows recently finalized IDs; waiting: %s",
                           ", ".join(sorted(stale_remote)))
            return False
        for oid, tracked in list(self.open_orders.items()):
            if oid in remote:
                self._observe(tracked, remote[oid])
            else:
                self.finalizing[oid] = tracked
            if tracked.expiration and time.time() >= tracked.expiration:
                # A stale LIVE list must not hide expiry. Read final fill status;
                # do not assume zero fills or free capacity until confirmed.
                self.finalizing[oid] = tracked
        self.finish_settlements()
        return True

    def finish_settlements(self):
        if self.config.dry_run:
            return True
        self._reconcile_finalizing_orders()
        self._refresh_pending_trades()
        return self._update_settlement_timeout()

    def _reconcile_finalizing_orders(self) -> None:
        """Reconcile final fills and terminal statuses for quarantined orders."""
        for oid, tracked in list(self.finalizing.items()):
            row = self.client.get_order(oid)
            try:
                self._observe(tracked, row)
            except (KeyError, TypeError, ValueError) as exc:
                # Order detail may lag a successful cancel response or temporarily
                # use a different schema. Keep the known order quarantined; the
                # timeout below turns a persistent problem into a controlled stop.
                logger.warning("Order detail unavailable for %s; keeping it quarantined: %s", oid, exc)
                continue
            if tracked.matched > 0:
                trades = row.get("associate_trades")
                if not trades:
                    continue
                self.pending_trades.update(str(t) for t in trades)
            status = str(row.get("status", "")).removeprefix("ORDER_STATUS_")
            if status in ("CANCELED", "CANCELLED", "MATCHED", "INVALID", "CANCELED_MARKET_RESOLVED", "EXPIRED"):
                self.open_orders.pop(oid, None)
                del self.finalizing[oid]
                self.cancel_acknowledged.discard(oid)
                self.recently_finalized[oid] = time.monotonic()
            elif status in ("LIVE", "DELAYED", "UNMATCHED"):
                # A cancel acknowledgement can arrive before the order-detail
                # replica reflects it. Keep the order quarantined: no replacement
                # order may be sent until a terminal status is observable.
                continue
            else:
                raise RuntimeError(f"Order {oid} has unresolved status {status}")

    def _refresh_pending_trades(self) -> None:
        """Refresh trade records that are needed before inventory can be trusted."""
        for trade_id in list(self.pending_trades):
            rows = self.client.get_trades(TradeParams(id=trade_id))
            row = next((r for r in rows if str(r.get("id")) == trade_id), None)
            if row and str(row.get("status", "")).removeprefix("TRADE_STATUS_") in ("CONFIRMED", "FAILED"):
                self.trade_history[trade_id] = row
                logger.info("Trade %s %s", trade_id, row["status"])
                self.pending_trades.remove(trade_id)
                self.dirty_inventory = True

    def _update_settlement_timeout(self) -> bool:
        """Track unresolved settlement age and report whether writes are safe."""
        pending = bool(self.finalizing or self.pending_trades)
        if pending:
            if self.pending_since is None:
                self.pending_since = time.monotonic()
            elif time.monotonic() - self.pending_since > self.config.settlement_timeout_seconds:
                raise OrderStateError("Settlement timed out; no replacement orders will be sent")
        else:
            self.pending_since = None
        return not pending

    def cancel_ids(self, ids):
        ids = [order_id for order_id in dict.fromkeys(ids)
               if order_id not in self.cancel_acknowledged]
        if not ids:
            return
        if self.config.dry_run:
            for oid in ids:
                self.open_orders.pop(oid, None)
                logger.info("DRY cancel %s", oid)
            return
        response = self.client.cancel_orders(ids)
        if not isinstance(response, dict) or not isinstance(response.get("canceled"), list):
            raise RuntimeError("Unconfirmed cancellation response")
        confirmed = set(response["canceled"])
        self.cancel_acknowledged.update(confirmed)
        for oid in ids:
            if oid in confirmed and oid in self.open_orders:
                # Retain the order until its final fill amount is read (cancel/fill race).
                self.finalizing[oid] = self.open_orders[oid]
                logger.info("Order cancelled id=%s", oid)
        self.finish_settlements()
        unresolved = set(ids) - confirmed
        if unresolved:
            # A filled/already-cancelled order can be reported as not_canceled while
            # its final status propagates. Quarantine it and retry cancellation on
            # later cycles instead of treating this single response as fatal.
            for oid in unresolved:
                if oid in self.open_orders:
                    self.finalizing[oid] = self.open_orders[oid]
            self.finish_settlements()
            still_open = sorted(oid for oid in unresolved if oid in self.open_orders)
            if still_open:
                logger.warning("Cancellation not yet confirmed for %s; orders quarantined", ", ".join(still_open))

    def cancel_market(self, condition_id):
        self.cancel_ids(
            o.order_id for o in self.open_orders.values()
            if o.condition_id == condition_id and o.order_id not in self.cancel_acknowledged
        )

    def cancel_all(self):
        if self.config.dry_run:
            self.open_orders.clear()
            return
        # Used only for startup/shutdown/emergency; no market keyword SDK mismatch.
        response = self.client.cancel_all()
        if not isinstance(response, dict) or not isinstance(response.get("canceled"), list):
            raise RuntimeError("Unconfirmed account cancellation")
        remaining = self.client.get_open_orders()
        if not isinstance(remaining, list) or remaining:
            raise RuntimeError("Account still has open orders after cancel-all")
        self.finalizing.update(self.open_orders)
        self.cancel_acknowledged.update(self.open_orders)
        self.open_orders.clear()
        self.dirty_inventory = True
        if not self.finish_settlements():
            raise RuntimeError("Account cancellation still propagating; retry cleanup")
        logger.info("Account orders confirmed cancelled")

    def cancel_expired(self):
        # TTL is a health watchdog, separate from scheduled quote replacement.
        if time.monotonic() - self.last_healthy >= self.config.order_ttl_seconds:
            self.cancel_ids(self.open_orders)

    def quote_expiration(self, inventory_reduction=False):
        if not inventory_reduction:
            return 0
        lifetime = max(self.config.inventory_quote_expiration_seconds,
                       self.config.gtd_min_expiration_seconds)
        return math.ceil(time.time() + lifetime)

    def refresh_quotes(self, condition_id, *, inventory_reduction=False):
        """Normal GTC refresh; recovery GTD orders expire on the exchange."""
        orders = [o for o in self.open_orders.values() if o.condition_id == condition_id]
        if inventory_reduction:
            # A normal opposite-side quote may survive the fill that starts recovery.
            # Replace it once with GTD, only after cancellation/fills are reconciled.
            ids = [o.order_id for o in orders if not o.expiration]
        else:
            orders = [o for o in orders if not o.expiration]
            if not orders or time.monotonic() - min(o.created_at for o in orders) < self.config.quote_refresh_seconds:
                return False
            ids = [o.order_id for o in orders]
        if not ids:
            return False
        self.cancel_ids(ids)
        logger.info("%s: cancelled %d orders for market %s",
                    "Recovery GTC to GTD migration" if inventory_reduction else "Regular quote refresh",
                    len(ids), condition_id)
        return True

    def cancel_inventory_forbidden(self, inventory):
        """Apply the same filled-inventory rule to every managed market's live orders."""
        ids = []
        for mid, tokens in inventory.tokens.items():
            position = inventory.positions[mid]
            outcomes = dict(zip(tokens, ("YES", "NO")))
            for order in self.open_orders.values():
                if order.token_id in outcomes and not inventory_allows_order(
                        position.yes, position.no, outcomes[order.token_id], order.side):
                    ids.append(order.order_id)
        self.cancel_ids(ids)
        return bool(ids)

    def cancel_obsolete(self, targets: dict[_OrderKey, _Target], condition_id=None):
        ids = [o.order_id for o in self.open_orders.values()
               if (condition_id is None or o.condition_id == condition_id)
               and self._order_is_obsolete(o, targets)]
        self.cancel_ids(ids)
        return bool(ids)

    @staticmethod
    def _target_parts(target: _Target) -> tuple[float, float, float | None]:
        """Return price, tick, and optional recovery size from the target tuple."""
        price, tick = target[:2]
        size = target[2] if len(target) == 3 else None
        return price, tick, size

    @classmethod
    def _order_is_obsolete(cls, order: TrackedOrder, targets: dict[_OrderKey, _Target]) -> bool:
        key = (order.token_id, order.side)
        if key not in targets:
            return True
        target = targets[key]
        price, _, size = cls._target_parts(target)
        return (
            abs(order.price - price) > _PRICE_EPSILON
            or (size is not None and order.remaining > size + _SIZE_EPSILON)
        )

    def place_missing(self, market, targets: dict[_OrderKey, _Target], *, inventory_reduction=False):
        """Place missing targets only when account and inventory state is settled.

        New writes are blocked while the manager is blocked, an order or trade is
        still being reconciled, or inventory may not yet include a recent fill.
        """
        if self.blocked or self.finalizing or self.pending_trades or self.dirty_inventory:
            raise OrderStateError("Order/inventory state must be synchronized before placing")
        if targets:
            self.keepalive()
        missing = []
        for (token, side), target in targets.items():
            price, tick, size = self._target_parts(target)
            if size is not None:
                if any(o.token_id == token and o.side == side for o in self.open_orders.values()):
                    continue
                if len(self.open_orders) < self.config.max_open_orders:
                    self._place(market.condition_id, token, side, price, tick, size=size,
                                inventory_reduction=inventory_reduction)
                continue
            if any(o.token_id == token and o.side == side for o in self.open_orders.values()):
                continue
            if len(self.open_orders) + len(missing) >= self.config.max_open_orders:
                break
            missing.append((token, side, price, tick))
        if len(missing) == 1 or self.config.dry_run:
            for token, side, price, tick in missing:
                self._place(market.condition_id, token, side, price, tick,
                            inventory_reduction=inventory_reduction)
        elif missing:
            self._place_batch(market.condition_id, missing, inventory_reduction=inventory_reduction)

    def _place_batch(self, condition_id, orders, *, inventory_reduction=False):
        """Sign locally, then post all currently missing quotes in one request."""
        signed = []
        expiration = self.quote_expiration(inventory_reduction)
        order_type = OrderType.GTD if inventory_reduction else OrderType.GTC
        for token, side, price, tick in orders:
            order = self.client.create_order(
                OrderArgs(token_id=token, price=price,
                          side=Side.BUY if side == "BUY" else Side.SELL,
                          size=self.config.order_size, expiration=expiration),
                PartialCreateOrderOptions(tick_size=str(tick)))
            signed.append(PostOrdersV2Args(order=order, orderType=order_type))
        try:
            responses = self.client.post_orders(signed, post_only=True)
        except Exception as exc:
            logger.error("%s batch placement failed: %s", order_type, exc)
            if (getattr(exc, "status_code", None) in (400, 401, 403, 404, 422, 425, 429)
                    and "duplicat" not in str(exc).lower()):
                raise RuntimeError("Quote batch explicitly rejected; retry only after fresh data") from exc
            self.blocked = True
            raise OrderStateError("Batch placement outcome unknown; cancel account orders and stop") from exc
        if not isinstance(responses, list) or len(responses) != len(orders):
            self.blocked = True
            raise OrderStateError("Malformed batch placement response")
        # Record every unambiguous accepted order before reporting a partial
        # rejection, so cleanup can cancel all known live orders safely.
        rejected = []
        for (token, side, price, _tick), response in zip(orders, responses):
            if not isinstance(response, dict):
                self.blocked = True
                raise OrderStateError("Malformed item in batch placement response")
            order_id = response.get("orderID") or response.get("id")
            if order_id and response.get("success") is not False and response.get("status") in (
                    "live", "LIVE", "ORDER_STATUS_LIVE"):
                self.open_orders[str(order_id)] = TrackedOrder(
                    str(order_id), token, side, price, self.config.order_size,
                    time.monotonic(), condition_id, expiration=expiration)
                self._log_placement(order_id, side, price, self.config.order_size,
                                    order_type, expiration, inventory_reduction)
            else:
                rejected.append(response.get("errorMsg", "missing live order ID"))
        if rejected:
            logger.error("%s batch rejected: %s", order_type, "; ".join(rejected))
            self.blocked = True
            raise OrderStateError("Partial batch placement; account cleanup required: " + "; ".join(rejected))

    def _place(self, condition_id, token, side, price, tick, size=None, stop=False,
               inventory_reduction=False):
        size = self.config.order_size if size is None else size
        expiration = 0 if stop else self.quote_expiration(inventory_reduction)
        order_type = OrderType.FAK if stop else (OrderType.GTD if inventory_reduction else OrderType.GTC)
        if self.config.dry_run:
            oid = "DRY-" + uuid4().hex
        else:
            # Signing is read-only. A failure here does not create an unknown order.
            signed = self.client.create_order(
                OrderArgs(token_id=token, price=price, side=Side.BUY if side == "BUY" else Side.SELL,
                          size=size, expiration=expiration),
                PartialCreateOrderOptions(tick_size=str(tick)))
            try:
                response = self.client.post_order(signed, order_type, post_only=not stop)
            except Exception as exc:
                logger.error("%s placement failed: %s", order_type, exc)
                if (getattr(exc, "status_code", None) in (400, 401, 403, 404, 422, 425, 429)
                        and "duplicat" not in str(exc).lower()):
                    raise RuntimeError("Order explicitly rejected; retry only after fresh data") from exc
                self.blocked = True
                raise OrderStateError("Placement outcome unknown; cancel account orders and stop") from exc
            if not isinstance(response, dict):
                self.blocked = True
                raise OrderStateError("Malformed placement response")
            oid = response.get("orderID") or response.get("id")
            if not oid and response.get("success") is False:
                logger.error("%s order rejected: %s", order_type, response.get("errorMsg"))
                # Explicit rejection is safe to retry only in a later bounded-error cycle.
                raise RuntimeError(f"Order rejected: {response.get('errorMsg', 'missing order ID')}")
            if not oid or response.get("success") is False:
                self.blocked = True
                raise OrderStateError("Placement result is ambiguous; recovery required")
            if not stop and response.get("status") not in ("live", "LIVE", "ORDER_STATUS_LIVE"):
                self.blocked = True
                raise OrderStateError("Unexpected post-only order status; recover before trading")
        self.open_orders[str(oid)] = TrackedOrder(
            str(oid), token, side, price, size, time.monotonic(), condition_id, expiration=expiration)
        if stop and not self.config.dry_run:
            self.finalizing[str(oid)] = self.open_orders[str(oid)]
            self.dirty_inventory = True
        self._log_placement(oid, side, price, size, order_type, expiration, inventory_reduction)

    def _log_placement(self, oid, side, price, size, order_type, expiration, inventory_reduction):
        mode = "STOP_LOSS" if order_type == OrderType.FAK else (
            "INVENTORY" if inventory_reduction else "NORMAL")
        logger.info("%sOrder placed id=%s side=%s price=%.4f size=%.4f type=%s mode=%s "
                    "expiration=%d remaining_seconds=%.1f",
                    "DRY " if self.config.dry_run else "", oid, side, price, size, order_type,
                    mode, expiration, max(0.0, expiration - time.time()) if expiration else 0.0)

    def close_unmatched_position(self, market, book, size):
        """Caller supplies freshly reconciled unmatched size after cancel confirmation."""
        if (self.blocked or self.finalizing or self.pending_trades or self.dirty_inventory
                or any(o.condition_id == market.condition_id for o in self.open_orders.values())):
            return False
        if len(self.open_orders) >= self.config.max_open_orders:
            return False
        if self.config.dry_run and market.market_id in self.dry_stops:
            return False
        from decimal import ROUND_FLOOR, ROUND_CEILING
        from quoting import round_to_tick
        size = round_to_tick(size, .01, ROUND_FLOOR)
        if size <= 0 or size < book.minimum_order_size or not book.bids:
            return False
        # Cross displayed depth down to the worst available bid, never a resting GTC.
        remaining = size
        limit = book.best_bid
        for level in book.bids:
            limit = level.price
            remaining -= level.size
            if remaining <= 0:
                break
        limit = round_to_tick(limit, book.tick_size, ROUND_FLOOR)
        floor = round_to_tick(max(book.tick_size, self.config.min_price), book.tick_size, ROUND_CEILING)
        ceiling = round_to_tick(min(1 - book.tick_size, self.config.max_price), book.tick_size, ROUND_FLOOR)
        limit = min(ceiling, max(floor, limit))
        if floor > ceiling or limit > book.best_bid:
            return False
        self.keepalive()
        self._place(market.condition_id, book.token_id, "SELL", limit, book.tick_size, size, stop=True)
        if self.config.dry_run:
            self.dry_stops.add(market.market_id)
        return True
