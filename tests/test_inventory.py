import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from inventory import InventoryTracker
from models import Position
from helpers import FakeClob


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inventory.csv"
        self.inventory = InventoryTracker(self.path)

    def test_balances_are_shares_and_atomic_csv_roundtrip(self):
        client = FakeClob()
        p = self.inventory.reconcile_from_clob(client, "a", "y", "n")
        self.assertEqual(p, Position(5, 5, 100))
        other = InventoryTracker(self.path)
        other.load_local()
        self.assertEqual(other.positions["a"], p)
        self.assertEqual(other.tokens["a"], ("y", "n"))
        self.assertFalse(self.path.with_suffix(".csv.tmp").exists())

    def test_startup_reconciliation_refreshes_every_saved_market(self):
        self.inventory.positions.update({
            "a": Position(7, 4, 20),
            "b": Position(1, 9, 20),
        })
        self.inventory.tokens.update({"a": ("a-y", "a-n"), "b": ("b-y", "b-n")})
        self.inventory.save()
        client = FakeClob()
        client.balances.update({"a-y": 2, "a-n": 3, "b-y": 4, "b-n": 5})

        self.inventory.reconcile_saved_positions(client)

        self.assertEqual(self.inventory.positions["a"], Position(2, 3, 100))
        self.assertEqual(self.inventory.positions["b"], Position(4, 5, 100))
        self.assertEqual(client.balance_calls, 5)  # One collateral + YES/NO per market.
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertEqual(loaded.positions, self.inventory.positions)

    def test_partial_request_failure_keeps_old_state_and_file(self):
        self.inventory.positions["a"] = Position(7, 4, 20)
        self.inventory.save()
        before = self.path.read_bytes()
        client = Mock()
        client.get_balance_allowance.side_effect = [{"balance": "5000000"}, RuntimeError("outage")]
        with self.assertRaises(RuntimeError):
            self.inventory.reconcile_from_clob(client, "a", "y", "n")
        self.assertEqual(self.inventory.positions["a"], Position(7, 4, 20))
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_balance_not_zero(self):
        client = Mock()
        client.get_balance_allowance.return_value = {}
        with self.assertRaises(ValueError):
            self.inventory.reconcile_from_clob(client, "a", "y", "n")

    def test_legacy_snapshot_migration(self):
        self.path.write_text("market_id,yes,no,usdc\na,5000000,0,100000000\n")
        self.inventory.load_local()
        self.assertEqual(self.inventory.positions["a"], Position(5, 0, 100))

    def test_failed_save_rolls_back_memory_and_retries(self):
        from unittest.mock import patch
        client = FakeClob()
        with patch.object(self.inventory, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.inventory.reconcile_from_clob(client, "a", "y", "n")
        self.assertNotIn("a", self.inventory.positions)
        self.inventory.reconcile_from_clob(client, "a", "y", "n")
        self.assertTrue(self.path.exists())

    def trade(self, tid, size, price, side="BUY", **extra):
        self.inventory.trade_history[tid] = dict(id=tid, size=str(size), price=str(price), side=side,
            asset_id="y", trader_side="TAKER", match_time=int(tid), status="CONFIRMED", **extra)

    def test_weighted_average_survives_partial_sales_and_new_buys(self):
        self.trade("1", 5, .40)
        self.trade("2", 5, .60)
        self.assertAlmostEqual(self.inventory.average_cost("y", 10), .50)
        self.trade("3", 4, .30, "SELL")
        self.assertAlmostEqual(self.inventory.average_cost("y", 6), .50)
        self.trade("4", 4, .75)
        self.assertAlmostEqual(self.inventory.average_cost("y", 10), .60)

    def test_confirmed_only_and_idempotent_replay(self):
        self.trade("1", 5, .40)
        self.trade("2", 5, .60)
        self.inventory.trade_history["2"]["status"] = "MATCHED"
        self.assertEqual(self.inventory.average_cost("y", 5), .40)
        self.inventory.trade_history["2"]["status"] = "FAILED"
        self.assertEqual(self.inventory.average_cost("y", 5), .40)
        self.assertEqual(self.inventory.average_cost("y", 5), .40)

    def test_fixed_six_history_requires_matching_balance(self):
        self.trade("1", 5_000_000, .46)
        self.assertAlmostEqual(self.inventory.average_cost("y", 5), .46)
        self.assertIsNone(self.inventory.average_cost("y", 4))
        self.assertIn("y", self.inventory.inconsistent_tokens)
        self.assertAlmostEqual(self.inventory.average_cost("y", 5), .46)
        self.assertNotIn("y", self.inventory.inconsistent_tokens)

    def test_maker_uses_only_own_leg_and_its_token_price(self):
        self.inventory.trade_owner = "our-key"
        self.inventory.trade_history["1"] = dict(id="1", status="CONFIRMED", match_time=1,
            trader_side="MAKER", asset_id="n", side="BUY", maker_orders=[
                dict(owner="other", asset_id="y", side="BUY", matched_amount="99", price=".99"),
                dict(owner="our-key", asset_id="y", side="BUY", matched_amount="5", price=".46")])
        self.assertAlmostEqual(self.inventory.average_cost("y", 5), .46)
        self.assertIsNone(self.inventory.average_cost("n", 5))

    def test_saved_cost_roundtrip_but_not_trusted_over_real_history(self):
        self.trade("1", 5, .46)
        client = FakeClob()
        client.balances.update({"y": 5, "n": 0})
        self.inventory.reconcile_from_clob(client, "a", "y", "n")
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertAlmostEqual(loaded.positions["a"].avg_entry_yes, .46)
        loaded.reconcile_from_clob(client, "a", "y", "n")
        self.assertIsNone(loaded.positions["a"].avg_entry_yes)

    def test_missing_history_never_uses_current_book_or_saved_cost(self):
        self.assertIsNone(self.inventory.average_cost("y", 5))
        self.trade("1", 5, .46)
        self.assertIsNone(self.inventory.average_cost("y", 0))
        self.assertIn("y", self.inventory.inconsistent_tokens)

    def empty_snapshot(self):
        from orders import OrderManager
        from helpers import config
        client = FakeClob()
        client.balances.update({"a-y": 0, "a-n": 0})
        self.inventory.reconcile_from_clob(client, "a", "a-y", "a-n")
        orders = OrderManager(client, config(dry_run=False))
        orders.dirty_inventory = False
        return orders

    def test_prune_empty_removes_memory_and_csv_but_not_shared_collateral(self):
        orders = self.empty_snapshot()
        self.assertEqual(self.inventory.positions["a"].usdc, 100)
        self.assertEqual(self.inventory.prune_empty_markets(orders), ["a"])
        self.assertFalse(self.inventory.positions)
        self.assertFalse(self.inventory.tokens)
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertFalse(loaded.positions)

    def test_prune_preserves_paired_and_dust_positions(self):
        orders = self.empty_snapshot()
        for mid, yes, no in (("paired", 5, 5), ("dust", 0, .004407), ("held", 1, 0)):
            orders.client.balances.update({mid + "-y": yes, mid + "-n": no})
            self.inventory.reconcile_from_clob(orders.client, mid, mid + "-y", mid + "-n")
        self.assertEqual(self.inventory.prune_empty_markets(orders), ["a"])
        self.assertEqual(set(self.inventory.positions), {"paired", "dust", "held"})

    def test_prune_never_trusts_a_locally_loaded_zero(self):
        orders = self.empty_snapshot()
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertEqual(loaded.prune_empty_markets(orders), [])
        self.assertIn("a", loaded.positions)

    def test_prune_preserves_active_or_inconsistent_markets(self):
        orders = self.empty_snapshot()
        self.assertEqual(self.inventory.prune_empty_markets(orders, {"a"}), [])
        self.inventory.inconsistent_tokens.add("a-y")
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        self.assertIn("a", self.inventory.positions)

    def test_prune_waits_for_orders_and_settlement(self):
        from orders import TrackedOrder
        orders = self.empty_snapshot()
        orders.open_orders["x"] = TrackedOrder("x", "a-y", "BUY", .4, 5, 0, "condition-a")
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        orders.finalizing["x"] = orders.open_orders.pop("x")
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        orders.finalizing.clear()
        orders.pending_trades.add("fill")
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        orders.pending_trades.clear()
        orders.dirty_inventory = True
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        orders.dirty_inventory = False
        orders.blocked = True
        self.assertEqual(self.inventory.prune_empty_markets(orders), [])
        self.assertIn("a", self.inventory.positions)

    def test_failed_prune_save_restores_memory_and_leaves_file(self):
        from unittest.mock import patch
        orders = self.empty_snapshot()
        self.inventory.stops["a"] = "YES"
        self.inventory.save()
        before = self.path.read_bytes()
        with patch.object(self.inventory, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.inventory.prune_empty_markets(orders)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("a", self.inventory.positions)
        self.assertIn("a", self.inventory.tokens)
        self.assertIn("a", self.inventory.reconciled_markets)
        self.assertEqual(self.inventory.stops["a"], "YES")
