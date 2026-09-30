from __future__ import annotations

import csv
import logging
import math
import os
from pathlib import Path
from typing import Any, Iterable

from py_clob_client_v2 import BalanceAllowanceParams, AssetType
from models import Position
from quoting import inventory_mode

logger = logging.getLogger(__name__)

_BALANCE_SCALE = 1_000_000
_SHARE_TOLERANCE = 1e-8
_BALANCE_TOLERANCE = 1e-7
_CSV_VERSION = 3
_CSV_COLUMNS = (
    "market_id", "yes", "no", "usdc", "yes_token_id", "no_token_id", "version",
    "avg_entry_yes", "avg_entry_no", "stop_side",
)
TradeLeg = tuple[str, float, float]


class InventoryTracker:
    """Atomic CSV snapshots in shares/collateral units, never raw 6-decimal balances."""
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else Path(__file__).with_name("inventory.csv")
        self.positions: dict[str, Position] = {}
        self.tokens: dict[str, tuple[str, str]] = {}
        self.trade_history: dict[str, dict[str, Any]] = {}
        self.trade_owner = ""
        self.funder = ""
        self.stops: dict[str, str] = {}
        self.inconsistent_tokens: set[str] = set()
        self.reconciled_markets: set[str] = set()

    @staticmethod
    def _replay_cost(legs: Iterable[TradeLeg], balance: float, scale: float) -> float | None:
        """Replay ordered fills at one unit scale and match the live balance."""
        quantity = cost = 0.0
        for side, raw_size, price in legs:
            size = raw_size / scale
            if side == "BUY":
                quantity, cost = quantity + size, cost + size * price
            elif size > quantity + _SHARE_TOLERANCE:
                return None
            else:
                cost = cost * max(0, quantity - size) / quantity if quantity else 0
                quantity = max(0, quantity - size)
        if abs(quantity - balance) <= _BALANCE_TOLERANCE:
            return cost / quantity if quantity > 0 else None
        return None

    def average_cost(self, token: str, balance: float) -> float | None:
        """Replay authenticated fills and return a matching weighted-average cost.

        Decimal and fixed-six history are both accepted only when they explain the
        live balance. Transfers, fees, and incomplete histories fail closed.
        """
        self.inconsistent_tokens.discard(token)
        legs: list[TradeLeg] = []
        try:
            rows = sorted(
                self.trade_history.values(),
                key=lambda row: (float(row.get("match_time", 0)), str(row["id"])),
            )
            for row in rows:
                if str(row.get("status", "")).removeprefix("TRADE_STATUS_") != "CONFIRMED":
                    continue
                if row.get("trader_side") == "TAKER":
                    own_legs = [row]
                elif row.get("trader_side") == "MAKER":
                    own_legs = [
                        leg for leg in row.get("maker_orders", [])
                        if (self.trade_owner and leg.get("owner") == self.trade_owner)
                        or (self.funder and str(leg.get("maker_address", "")).lower() == self.funder.lower())
                    ]
                else:
                    continue

                for leg in own_legs:
                    if leg.get("asset_id") != token:
                        continue
                    size = float(leg.get("matched_amount", leg.get("size")))
                    price = float(leg["price"])
                    if not math.isfinite(size) or size <= 0 or not 0 < price < 1:
                        return None
                    side = leg.get("side")
                    if side is None and row.get("trader_side") == "MAKER":
                        side = row.get("side")
                        if leg["asset_id"] == row.get("asset_id"):
                            side = {"BUY": "SELL", "SELL": "BUY"}.get(side)
                    if side not in ("BUY", "SELL"):
                        return None
                    legs.append((side, size, price))

            for scale in (1, _BALANCE_SCALE):
                cost = self._replay_cost(legs, balance, scale)
                if cost is not None:
                    return cost
            if legs:
                self.inconsistent_tokens.add(token)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        return None

    def load_local(self) -> None:
        """Load saved positions, token IDs, entry costs, and recovery stops."""
        self.reconciled_markets.clear()
        if not self.path.exists():
            return
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                # Old snapshots were raw API balances. They are not used for trading.
                scale = 1 if row.get("version") in ("2", "3") else _BALANCE_SCALE
                p = Position(*(float(row[k]) / scale for k in ("yes", "no", "usdc")))
                if any(not math.isfinite(v) or v < 0 for v in (p.yes, p.no, p.usdc)):
                    raise ValueError("Invalid local inventory")
                self.positions[row["market_id"]] = p
                for field in ("avg_entry_yes", "avg_entry_no"):
                    if row.get(field):
                        value = float(row[field])
                        if not 0 < value < 1:
                            raise ValueError("Invalid saved entry cost")
                        setattr(p, field, value)
                if row.get("stop_side") in ("YES", "NO"):
                    self.stops[row["market_id"]] = row["stop_side"]
                if row.get("yes_token_id") and row.get("no_token_id"):
                    self.tokens[row["market_id"]] = (row["yes_token_id"], row["no_token_id"])

    def _csv_row(self, market_id: str, position: Position) -> list[Any]:
        yes_token_id, no_token_id = self.tokens.get(market_id, ("", ""))
        return [
            market_id,
            position.yes,
            position.no,
            position.usdc,
            yes_token_id,
            no_token_id,
            _CSV_VERSION,
            position.avg_entry_yes,
            position.avg_entry_no,
            self.stops.get(market_id, ""),
        ]

    def save(self) -> None:
        """Persist the current inventory snapshot using an atomic file replace."""
        temporary = self.path.with_suffix(".csv.tmp")
        try:
            with temporary.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(_CSV_COLUMNS)
                for market_id, p in self.positions.items():
                    writer.writerow(self._csv_row(market_id, p))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _balance(
            client: Any,
            asset_type: Any,
            token_id: str | None,
            signature_type: int,
    ) -> float:
        info = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=asset_type, token_id=token_id, signature_type=signature_type))
        if not isinstance(info, dict) or "balance" not in info:
            raise ValueError("Missing balance in CLOB response")
        balance = float(info["balance"]) / _BALANCE_SCALE
        if not math.isfinite(balance) or balance < 0:
            raise ValueError("Invalid CLOB balance")
        return balance

    def collateral_balance(self, client: Any, signature_type: int) -> float:
        """Return the account-wide collateral balance used for reconciliation."""
        return self._balance(client, AssetType.COLLATERAL, None, signature_type)

    def _fetch_position(
            self,
            client: Any,
            yes_token_id: str,
            no_token_id: str,
            signature_type: int,
            collateral: float | None,
    ) -> Position:
        yes = self._balance(client, AssetType.CONDITIONAL, yes_token_id, signature_type)
        no = self._balance(client, AssetType.CONDITIONAL, no_token_id, signature_type)
        cash = (self._balance(client, AssetType.COLLATERAL, None, signature_type)
                if collateral is None else collateral)
        return Position(
            yes,
            no,
            cash,
            self.average_cost(yes_token_id, yes),
            self.average_cost(no_token_id, no),
        )

    def _log_mode_change(
            self,
            market_id: str,
            old: Position | None,
            position: Position,
    ) -> None:
        previous = inventory_mode(old.yes, old.no) if old else None
        mode = inventory_mode(position.yes, position.no)
        if previous == mode:
            return
        permission = {
            "BALANCED": "Two-sided quoting allowed",
            "LONG_YES": "Only NO buying allowed",
            "LONG_NO": "Only YES buying allowed",
        }[mode]
        logger.info(
            "Inventory mode changed market=%s: %s -> %s YES=%.6f NO=%.6f net=%.6f; %s",
            market_id,
            previous or "UNKNOWN",
            mode,
            position.yes,
            position.no,
            position.yes - position.no,
            permission,
        )

    def reconcile_from_clob(
            self,
            client: Any,
            market_id: str,
            yes_token_id: str,
            no_token_id: str,
            signature_type: int = -1,
            collateral: float | None = None,
    ) -> Position:
        # Commit nothing unless ALL requests succeed; callers must pause on errors.
        position = self._fetch_position(
            client, yes_token_id, no_token_id, signature_type, collateral)
        old = self.positions.get(market_id)
        old_tokens = self.tokens.get(market_id)
        token_pair = (yes_token_id, no_token_id)
        self.positions[market_id] = position
        self.tokens[market_id] = token_pair
        if old != position or old_tokens != token_pair:
            try:
                self.save()
            except Exception:
                if old is None:
                    self.positions.pop(market_id, None)
                else:
                    self.positions[market_id] = old
                if old_tokens is None:
                    self.tokens.pop(market_id, None)
                else:
                    self.tokens[market_id] = old_tokens
                raise
        if old != position:
            logger.info(
                "Inventory market=%s YES=%.4f NO=%.4f collateral=%.4f",
                market_id,
                position.yes,
                position.no,
                position.usdc,
            )
        self._log_mode_change(market_id, old, position)
        self.reconciled_markets.add(market_id)
        return position

    def prune_empty_markets(
            self,
            orders: Any,
            protected_market_ids: Iterable[str] = (),
    ) -> list[str]:
        """Remove confirmed empty, inactive snapshots only after order settlement.

        Net zero is insufficient: paired shares and dust are still positions.
        Locally loaded rows alone never authorize deletion.
        """
        if orders.blocked or orders.dirty_inventory or orders.finalizing or orders.pending_trades:
            return []
        protected = set(protected_market_ids)
        occupied_tokens = {o.token_id for o in orders.open_orders.values()}
        removed = [mid for mid, p in self.positions.items()
                   if p.yes == 0 and p.no == 0
                   and mid in self.reconciled_markets and mid in self.tokens
                   and mid not in protected
                   and not (set(self.tokens[mid]) & (occupied_tokens | self.inconsistent_tokens))]
        if not removed:
            return []
        positions = {mid: self.positions.pop(mid) for mid in removed}
        tokens = {mid: self.tokens.pop(mid) for mid in removed}
        stops = {mid: self.stops.pop(mid) for mid in removed if mid in self.stops}
        try:
            self.save()
        except Exception:
            self.positions.update(positions)
            self.tokens.update(tokens)
            self.stops.update(stops)
            raise
        self.reconciled_markets.difference_update(removed)
        logger.info("Removed %d empty saved markets (YES=0 NO=0, no open orders): %s",
                    len(removed), ", ".join(removed))
        return removed

    def reconcile_saved_positions(self, client: Any, signature_type: int = -1) -> None:
        """Refresh every saved market with token IDs before a bot run.

        The collateral balance is account-wide, so read it once and use that same
        authoritative snapshot for every CSV row. This corrects stale rows without
        adding a collateral request per historical market.
        """
        saved = tuple(self.tokens.items())
        logger.info("Startup inventory check: reconciling %d saved markets", len(saved))
        if not saved:
            return
        collateral = self._balance(client, AssetType.COLLATERAL, None, signature_type)
        for market_id, (yes_token_id, no_token_id) in saved:
            self.reconcile_from_clob(client, market_id, yes_token_id, no_token_id,
                                     signature_type, collateral)
        logger.info("Startup inventory check complete: %d saved markets updated", len(saved))
