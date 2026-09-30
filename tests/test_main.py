import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory import InventoryTracker
from models import Position
from bot import Bot
from main import configure_logging
from orders import OrderStateError
from helpers import config, market, FakeClob


class TestBot(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.client = FakeClob()
        self.bot = Bot(config(dry_run=False), self.client, InventoryTracker(Path(temp.name) / "inv.csv"))
        self.bot.initialize()
        self.bot.install_candidates([market("a"), market("b")])
        self.bot.step()  # Select a and establish first snapshot.
        self.bot.step()  # Quote after a second stable snapshot.

    def test_balanced_quotes_are_fixed_five(self):
        self.assertEqual(len(self.client.posts), 4)
        self.assertTrue(all(o.size == 5 for o in self.client.posts))

    def test_startup_reconciles_saved_inventory_but_manages_only_net_inventory(self):
        self.bot.inventory.positions.update({
            "zero": Position(0, 0, 100),
            "balanced": Position(10, 10, 100),
            "held": Position(0, 5, 100),
        })
        self.bot.inventory.tokens.update({
            "zero": ("zero-y", "zero-n"),
            "balanced": ("balanced-y", "balanced-n"),
            "held": ("held-y", "held-n"),
        })
        self.bot.inventory.save()
        self.client.balances.update({"held-y": 0, "held-n": 5})
        fresh = Bot(self.bot.config, self.client, InventoryTracker(self.bot.inventory.path))
        fresh.initialize()
        before = self.client.balance_calls
        with patch("bot.restore_inventory_markets", return_value={"held": market("held")}):
            fresh.step()
        # One account-wide collateral read, then YES/NO for all four saved markets.
        self.assertEqual(self.client.balance_calls, before + 9)
        self.assertIn("held", fresh.inventory_markets)
        self.assertNotIn("zero", fresh.inventory_markets)
        self.assertNotIn("balanced", fresh.inventory_markets)

    def test_unchanged_book_no_churn_or_balance_requests(self):
        count, balances, books = len(self.client.posts), self.client.balance_calls, self.client.book_calls
        for _ in range(4):
            self.bot.step()
        self.assertEqual(len(self.client.posts), count)
        self.assertEqual(self.client.balance_calls, balances)
        self.assertEqual(self.client.book_calls, books + 4)
        self.assertFalse(self.bot.orders.finalizing)

    def test_spread_collapse_switches_from_memory_after_cancel(self):
        self.client.bid, self.client.ask = .44, .45
        self.bot.step()
        self.assertIsNone(self.bot.market)
        self.assertFalse(self.client.get_open_orders())
        self.client.bid, self.client.ask = .40, .44
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "b")
        self.assertFalse(self.client.get_open_orders())

    def test_failed_cancel_waits_to_switch_and_never_requotes(self):
        self.client.refuse_cancel = True
        self.client.bid, self.client.ask = .44, .45
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "a")
        self.assertTrue(self.bot.exit_reason)
        self.bot.step()
        self.assertEqual(len(self.client.posts), 4)
        self.client.refuse_cancel = False
        self.bot.step()
        self.assertIsNone(self.bot.market)

    def test_acknowledged_cancel_with_stale_detail_waits_to_switch(self):
        original_get_order = self.client.get_order
        self.client.get_order = lambda oid: {**original_get_order(oid), "status": "LIVE"}
        self.client.bid, self.client.ask = .44, .45
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "a")
        self.assertTrue(self.bot.exit_reason)
        self.assertEqual(len(self.client.posts), 4)
        self.client.get_order = original_get_order
        self.bot.step()
        self.assertIsNone(self.bot.market)

    def test_yellow_cancels_then_recovers(self):
        self.client.bid, self.client.ask = .45, .49
        self.bot.step()
        self.assertEqual(self.bot.health.state, "YELLOW")
        self.assertFalse(self.client.get_open_orders())
        self.bot.step()
        self.assertEqual(self.bot.health.state, "YELLOW")
        self.bot.step()
        self.assertEqual(self.bot.health.state, "GREEN")
        self.assertEqual(len(self.client.get_open_orders()), 4)

    def test_fill_sync_blocks_while_settling(self):
        buy = next(oid for oid, row in self.client.rows.items() if row["side"] == "BUY")
        self.client.fill(buy, 5, "MATCHED")
        self.bot.step()
        self.assertFalse(self.client.get_open_orders())
        count = len(self.client.posts)
        self.bot.step()
        self.assertEqual(len(self.client.posts), count)
        self.client.trades["trade-" + buy]["status"] = "CONFIRMED"
        self.client.balances["a-y"] = 10
        self.bot.step()
        self.assertEqual(self.bot.inventory.positions["a"].yes, 10)
        self.assertNotIn(("a-y", "BUY"),
                         {(o.token_id, o.side) for o in self.bot.orders.open_orders.values()})

    def test_balance_outage_does_not_mutate_inventory(self):
        old = self.bot.inventory.positions["a"]
        self.bot.orders.dirty_inventory = True
        self.client.fail_balance = True
        with self.assertRaises(RuntimeError):
            self.bot.step()
        self.assertEqual(self.bot.inventory.positions["a"], old)
        self.assertFalse(self.client.get_open_orders())

    def test_scheduled_scan_keeps_healthy_active_market(self):
        self.bot.install_candidates([market("b"), market("a")])
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "a")
        self.assertEqual(len(self.client.posts), 4)

    def test_settlement_timeout_is_bounded(self):
        buy = next(oid for oid, row in self.client.rows.items() if row["side"] == "BUY")
        self.client.fill(buy, 2, "MINED")
        self.bot.step()
        self.bot.orders.pending_since -= 1000
        with self.assertRaises(OrderStateError):
            self.bot.step()

    def test_logging_level(self):
        with patch("main.logging.basicConfig") as setup:
            configure_logging(config(log_level="DEBUG"))
        self.assertEqual(setup.call_args.kwargs["level"], 10)

    def test_active_market_removed_by_scan_exits_cleanly(self):
        self.bot.install_candidates([market("b")])
        self.bot.step()
        self.assertIsNone(self.bot.market)
        self.assertFalse(self.client.get_open_orders())
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "b")

    def test_invalid_reserve_is_skipped_without_gamma_scan(self):
        self.client.bid, self.client.ask = .44, .45
        self.bot.step()  # Leave a.
        self.bot.step()  # Reject b using the same book request path.
        self.assertIsNone(self.bot.market)
        self.assertFalse(self.bot.ranked_markets)

    def test_changed_price_cancels_once_then_replaces(self):
        balances = self.client.balance_calls
        self.client.bid, self.client.ask = .41, .45
        self.bot.step()
        self.assertFalse(self.client.get_open_orders())
        self.assertEqual(len(self.client.posts), 4)
        self.bot.step()
        self.assertEqual(len(self.client.posts), 8)
        self.assertEqual(self.client.balance_calls, balances)

    def test_regular_refresh_replaces_in_same_loop(self):
        for order in self.bot.orders.open_orders.values():
            order.created_at -= self.bot.config.quote_refresh_seconds
        self.bot.step()
        self.assertEqual(len(self.client.posts), 8)
        self.assertEqual(len(self.client.get_open_orders()), 4)

    def test_unconfirmed_refresh_cancel_quarantines_without_replacement(self):
        self.client.refuse_cancel = True
        for order in self.bot.orders.open_orders.values():
            order.created_at -= self.bot.config.quote_refresh_seconds
        self.bot.step()
        self.assertEqual(len(self.client.posts), 4)
        self.assertTrue(self.bot.orders.finalizing)
        self.client.refuse_cancel = False
        self.bot.step()
        self.assertFalse(self.client.get_open_orders())

    def seed_mismatch(self):
        trade = dict(id="old", status="CONFIRMED", trader_side="TAKER", asset_id="a-y",
                     side="BUY", size="2", price=".4", match_time=1)
        self.client.trades["old"] = trade
        self.bot.orders.trade_history["old"] = trade
        self.bot.orders.dirty_inventory = True

    def test_mismatch_quarantines_one_market_and_quotes_next_candidate(self):
        self.seed_mismatch()
        self.bot.step()
        self.bot.step()
        self.assertEqual(self.bot.quarantined_markets, {"a"})
        self.assertIn("a", self.bot.inventory_markets)
        self.assertEqual(self.bot.market.market_id, "b")
        self.assertTrue(self.bot.orders.open_orders)
        self.assertTrue(all(o.condition_id == "condition-b" for o in self.bot.orders.open_orders.values()))
        self.assertEqual(self.bot.inventory.positions["a"].yes, 5)

    def test_mismatch_with_unconfirmed_cancel_still_blocks_new_orders(self):
        self.seed_mismatch()
        self.client.refuse_cancel = True
        before = len(self.client.posts)
        self.bot.step()
        self.bot.step()
        self.assertEqual(len(self.client.posts), before)
        self.assertEqual(self.bot.market.market_id, "a")
        self.assertTrue(self.bot.orders.finalizing)

    def test_quarantine_recovers_after_history_refresh(self):
        self.seed_mismatch()
        self.bot.step()
        self.client.trades["old"] = {**self.client.trades["old"], "size": "5"}
        self.bot.orders.dirty_inventory = True
        self.bot.step()
        self.assertFalse(self.bot.quarantined_markets)
        self.assertNotIn("a", self.bot.inventory_markets)
        self.assertNotIn("a-y", self.bot.inventory.inconsistent_tokens)

    def test_zero_balance_history_mismatch_does_not_pin_active_market(self):
        self.seed_mismatch()
        self.client.balances.update({"a-y": 0, "a-n": 0})
        self.bot.step()
        self.bot.step()
        self.assertEqual(self.bot.market.market_id, "b")
        self.assertIn("a", self.bot.quarantined_markets)
        self.assertIn("a", self.bot.inventory.positions)

    def test_status_reports_quarantine_and_is_rate_limited(self):
        self.seed_mismatch()
        self.bot.step()
        with self.assertLogs("main", "INFO") as logs:
            self.bot.report_status()
            self.bot.report_status()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("quarantined=a", logs.output[0])

    def test_refresh_fill_race_does_not_replace(self):
        order_id = next(iter(self.bot.orders.open_orders))
        self.client.fill(order_id, 2, "MINED")
        for order in self.bot.orders.open_orders.values():
            order.created_at -= self.bot.config.quote_refresh_seconds
        self.bot.step()
        self.assertEqual(len(self.client.posts), 4)
        self.assertTrue(self.bot.orders.dirty_inventory)
