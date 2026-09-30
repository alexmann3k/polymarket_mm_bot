import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace

from helpers import FakeClob, book, config, market
from inventory import InventoryTracker
from bot import Bot
from models import Position
from orders import TrackedOrder
from quoting import get_allowed_quote_sides, get_unmatched_position, recovery_price, should_trigger_stop_loss
from risk import RiskManager


class TestRecoveryLogic(unittest.TestCase):
    def test_configuration_float_conversion_and_validation(self):
        from config import Config
        with patch.dict("os.environ", {"STOP_LOSS_DISTANCE": "0.25", "MIN_PAIR_PROFIT": "0.02"}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.stop_loss_distance, .25)
        self.assertEqual(cfg.min_pair_profit, .02)
        for changes in ({"stop_loss_distance": 0}, {"stop_loss_distance": float("nan")},
                        {"min_pair_profit": -1}, {"min_pair_profit": 1}):
            with self.assertRaises(ValueError):
                config(**changes)

    def targets(self, p, orders=(), minimum=1, **changes):
        return RiskManager(config(**changes)).targets(market(),
            (book("a-y", minimum=minimum), book("a-n", minimum=minimum)), p,
            SimpleNamespace(positions={"a": p}), orders)

    def test_scenarios_a_through_e(self):
        for y, n, sides in ((0, 0, {"YES": True, "NO": True}),
                            (1, 0, {"YES": False, "NO": True}),
                            (0, 1, {"YES": True, "NO": False}),
                            (3, 1, {"YES": False, "NO": True}),
                            (2, 2, {"YES": True, "NO": True})):
            with self.subTest(yes=y, no=n):
                self.assertEqual(get_allowed_quote_sides(y, n), sides)
                targets = self.targets(Position(y, n, 100, .46, .46))
                self.assertEqual({token for token, side in targets if side == "BUY"},
                                 {"a-" + side[0].lower() for side, allowed in sides.items() if allowed})

    def test_recovery_price_accepts_lower_and_caps_upper(self):
        self.assertEqual(recovery_price(book(), .46, config()), .41)
        self.assertEqual(recovery_price(book(), .60, config()), .39)
        self.assertEqual(recovery_price(book(), .601, config()), .38)
        self.assertIsNone(recovery_price(book(), .99, config()))
        self.assertIsNone(recovery_price(book(), None, config()))

    def test_remaining_size_and_no_overhedge(self):
        for y, n, expected in ((10, 0, 5), (10, 4, 5), (7, 3, 4), (10, 9, 1)):
            self.assertEqual(self.targets(Position(y, n, 100, .46, .46))[("a-n", "BUY")][2], expected)

    def test_subminimum_pauses_but_keeps_existing_small_remainder(self):
        p = Position(7, 3, 100, .46, .46)
        self.assertFalse(self.targets(p, minimum=5))
        old = TrackedOrder("old", "a-n", "BUY", .41, 5, 0, "condition-a", matched=1)
        self.assertEqual(self.targets(p, [old], minimum=5)[("a-n", "BUY")][2], 4)

    def test_unknown_cost_fails_closed(self):
        self.assertFalse(self.targets(Position(5, 0, 100)))

    def test_epsilon_is_not_exchange_minimum(self):
        self.assertIsNone(get_unmatched_position(Position(1, 1 + 1e-10)))
        self.assertIsNone(get_unmatched_position(Position(.004407, 0)))
        self.assertIsNotNone(get_unmatched_position(Position(.02, 0)))

    def test_yes_and_no_stop_and_nonpositive_threshold(self):
        books = (book(bid=.29), book("n", bid=.29))
        self.assertTrue(should_trigger_stop_loss(Position(5, 0, 100, .60), books, .30))
        self.assertTrue(should_trigger_stop_loss(Position(0, 5, 100, None, .60), books, .30))
        self.assertFalse(should_trigger_stop_loss(Position(5, 5, 100, .60, .60), books, .30))
        self.assertFalse(should_trigger_stop_loss(Position(5, 0, 100, .20), books, .30))

    def test_risk_limits_remain_active(self):
        for changes in ({"max_total_capital": 9}, {"max_position_per_side": 5}):
            self.assertFalse(self.targets(Position(10, 5, 100, .46, .46), **changes))
        self.assertFalse(self.targets(Position(5, 0, 0, .46)))
        other = TrackedOrder("b", "b-y", "BUY", .41, 5, 0, "condition-b")
        self.assertFalse(self.targets(Position(5, 0, 100, .46), [other], max_open_orders=1))
        self.assertFalse(self.targets(Position(5, 0, 100, .46), [other], max_total_capital=14))


class TestRecoveryLoop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "inventory.csv"
        self.client = FakeClob()
        self.client.balances.update({"a-y": 0, "a-n": 0, "b-y": 0, "b-n": 0})
        self.bot = Bot(config(dry_run=False), self.client, InventoryTracker(self.path))
        self.bot.initialize()
        self.bot.install_candidates([market("a")])
        self.bot.step()
        self.bot.step()

    def buys(self, mid="a"):
        return {o.token_id: o.order_id for o in self.bot.orders.open_orders.values()
                if o.side == "BUY" and o.condition_id == "condition-" + mid}

    def cycles(self, count=3):
        for _ in range(count):
            self.bot.step()

    def fill(self, token, amount):
        self.client.fill(self.buys()[token], amount)
        self.bot.step()

    def seed_position(self, yes, no, entry=.46):
        self.bot.orders.cancel_market("condition-a")
        if self.bot.market is None and "a" not in self.bot.inventory_markets:
            self.bot.retain_market(market("a"))
        self.client.trades.clear()
        self.bot.orders.trade_history.clear()
        for token, qty in (("a-y", yes), ("a-n", no)):
            self.client.balances[token] = qty
            if qty:
                trade = dict(id="seed-" + token, trader_side="TAKER", asset_id=token,
                             side="BUY", size=str(qty), price=str(entry), status="CONFIRMED", match_time=-1)
                self.client.trades[trade["id"]] = trade
                self.bot.orders.trade_history[trade["id"]] = trade
        self.bot.orders.dirty_inventory = True
        self.cycles()

    def add_recovery(self, mid, yes, no):
        self.bot.inventory.positions[mid] = Position(yes, no, 100, .46, .46)
        self.bot.inventory.tokens[mid] = (mid + "-y", mid + "-n")
        for token, qty in ((mid + "-y", yes), (mid + "-n", no)):
            self.client.balances[token] = qty
            if qty:
                trade = dict(id="seed-" + token, trader_side="TAKER", asset_id=token,
                             side="BUY", size=str(qty), price=".46", status="CONFIRMED", match_time=-1)
                self.client.trades[trade["id"]] = trade
                self.bot.orders.trade_history[trade["id"]] = trade
        self.bot.retain_market(market(mid))

    def test_all_recoveries_and_exactly_one_normal_market_quote_together(self):
        self.fill("a-n", 5)
        self.add_recovery("held", 5, 0)
        self.bot.install_candidates([market("b"), market("c"), market("a"), market("held")])
        self.cycles()
        self.assertEqual(set(self.bot.inventory_markets), {"a", "held"})
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertEqual(set(self.buys("a")), {"a-y"})
        self.assertEqual(set(self.buys("held")), {"held-n"})
        self.assertEqual(set(self.buys("b")), {"b-y", "b-n"})
        self.assertFalse(self.buys("c"))
        before = {o.order_id for o in self.bot.orders.open_orders.values()}
        self.cycles()
        self.assertEqual({o.order_id for o in self.bot.orders.open_orders.values()}, before)

    def test_fill_in_normal_market_joins_existing_recoveries(self):
        self.fill("a-n", 5)
        self.bot.install_candidates([market("b"), market("c")])
        self.client.balances.update({"c-y": 0, "c-n": 0})
        self.cycles()
        old_recovery = self.buys("a")
        self.client.fill(self.buys("b")["b-y"], 5)
        self.cycles()
        self.assertEqual(set(self.bot.inventory_markets), {"a", "b"})
        self.assertEqual(self.bot.active_market.market_id, "c")
        self.assertEqual(self.buys("a"), old_recovery)
        self.assertEqual(set(self.buys("b")), {"b-n"})
        self.assertEqual(set(self.buys("c")), {"c-y", "c-n"})

    def test_paused_recovery_does_not_starve_other_recovery_or_normal_market(self):
        self.fill("a-n", 5)
        self.add_recovery("held", 1, 0)  # Below market minimum, still retained.
        self.bot.install_candidates([market("b")])
        self.cycles()
        self.assertIn("held", self.bot.inventory_markets)
        self.assertFalse(self.buys("held"))
        self.assertEqual(set(self.buys("a")), {"a-y"})
        self.assertEqual(set(self.buys("b")), {"b-y", "b-n"})

    def test_unfilled_orders_are_not_inventory(self):
        before = self.buys()
        self.cycles()
        self.assertEqual(before, self.buys())
        self.assertEqual(self.bot.inventory.positions["a"], Position(0, 0, 100))

    def test_empty_active_market_is_saved_until_exit_then_pruned(self):
        self.bot.orders.cancel_market("condition-a")
        self.assertEqual(self.bot.prune_empty_saved_markets(), [])
        self.assertIn("a", self.bot.inventory.positions)
        self.bot.install_candidates([market("b")])
        self.bot.step()
        self.assertIsNone(self.bot.market)
        self.assertNotIn("a", self.bot.inventory.positions)
        self.assertNotIn("a", self.bot.inventory.tokens)
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertNotIn("a", loaded.positions)

    def test_startup_verifies_stale_empty_rows_before_pruning(self):
        self.bot.inventory.positions.update({"empty": Position(), "stale": Position()})
        self.bot.inventory.tokens.update({"empty": ("empty-y", "empty-n"), "stale": ("stale-y", "stale-n")})
        self.bot.inventory.save()
        self.client.balances.update({"empty-y": 0, "empty-n": 0, "stale-y": 5, "stale-n": 5})
        fresh = Bot(self.bot.config, self.client, InventoryTracker(self.path))
        fresh.initialize()
        fresh.step()
        self.assertNotIn("empty", fresh.inventory.positions)
        self.assertEqual(fresh.inventory.positions["stale"].yes, 5)
        self.assertEqual(fresh.inventory.positions["stale"].no, 5)

    def test_full_yes_fill_keeps_only_safely_sized_no(self):
        before = self.buys()
        self.fill("a-y", 5)
        self.assertEqual(set(self.buys()), {"a-n"})
        self.assertNotEqual(self.buys()["a-n"], before["a-n"])
        self.assertGreater(self.bot.orders.open_orders[self.buys()["a-n"]].expiration, 0)
        self.assertEqual(self.bot.inventory.positions["a"].avg_entry_yes, .41)

    def test_full_no_fill_is_symmetric(self):
        before = self.buys()
        self.fill("a-n", 5)
        self.assertEqual(set(self.buys()), {"a-y"})
        self.assertNotEqual(self.buys()["a-y"], before["a-y"])

    def test_recovery_loop_uses_longer_lifetime_then_requotes(self):
        self.fill("a-y", 5)
        oid = self.buys()["a-n"]
        self.bot.orders.open_orders[oid].created_at -= self.bot.config.quote_refresh_seconds
        self.bot.step()
        self.assertEqual(self.buys()["a-n"], oid)
        self.client.rows[oid]["status"] = "EXPIRED"
        self.bot.step()
        self.assertNotEqual(self.buys()["a-n"], oid)
        self.assertEqual(self.client.rows[oid]["status"], "EXPIRED")
        new_oid = self.buys()["a-n"]
        self.assertGreater(self.bot.orders.open_orders[new_oid].expiration, 0)
        self.client.rows[new_oid]["status"] = "EXPIRED"
        self.bot.step()
        self.assertNotEqual(self.buys()["a-n"], new_oid)
        self.fill("a-n", 5)
        self.assertNotIn("a", self.bot.inventory_markets)

    def test_recovery_health_cancellation_does_not_wait_for_lifetime(self):
        self.fill("a-y", 5)
        oid = self.buys()["a-n"]
        self.bot.orders.open_orders[oid].created_at -= 10
        self.client.bid, self.client.ask = .40, .51  # Maximum-spread rule still applies.
        self.bot.step()
        self.assertFalse(self.buys())
        self.assertEqual(self.client.rows[oid]["status"], "CANCELED")

    def test_narrow_spread_keeps_recovery_order_until_routine_lifetime(self):
        self.fill("a-y", 5)
        oid = self.buys()["a-n"]
        self.bot.orders.open_orders[oid].created_at -= 10
        self.client.bid, self.client.ask = .42, .429  # Narrow, without a volatility jump.
        self.bot.step()
        self.assertEqual(self.buys(), {"a-n": oid})
        self.assertEqual(self.bot.orders.open_orders[oid].price, .41)
        self.assertEqual(self.bot.inventory_health["a"].state, "GREEN")
        self.bot.orders.open_orders[oid].created_at -= 30
        self.bot.step()
        self.assertEqual(self.buys(), {"a-n": oid})
        self.client.rows[oid]["status"] = "EXPIRED"
        self.bot.step()
        self.assertEqual(self.client.rows[oid]["status"], "EXPIRED")
        # Existing quote generation can wait if no new quote fits the tight book.
        self.assertNotIn(oid, self.bot.orders.open_orders)

    def test_partial_fill_cancels_overweight_and_oversized_hedge(self):
        before = self.buys()
        self.fill("a-y", 1)
        self.cycles()
        self.assertFalse(self.buys())
        for oid in before.values():
            self.assertEqual(self.client.rows[oid]["status"], "CANCELED")
        self.assertEqual(len(self.client.posts), 2)

    def test_smaller_valid_recovery_size_is_submitted(self):
        self.client.minimum_order_size = 1
        self.fill("a-y", 1)
        self.cycles()
        self.assertEqual(set(self.buys()), {"a-n"})
        self.assertEqual(self.client.posts[-1].size, 1)

    def test_partial_hedge_keeps_only_remaining_quantity(self):
        self.fill("a-y", 5)
        oid = self.buys()["a-n"]
        self.fill("a-n", 2)
        self.cycles()
        self.assertEqual(self.buys(), {"a-n": oid})
        self.assertEqual(self.bot.orders.open_orders[oid].remaining, 3)
        self.assertEqual(len(self.client.posts), 3)  # Two normal quotes + one GTD migration.

    def test_paired_recovery_is_released_after_order_cleanup(self):
        self.fill("a-y", 5)
        self.fill("a-n", 5)
        self.cycles()
        self.assertNotIn("a", self.bot.recovery_status)
        self.assertNotIn("a", self.bot.inventory_markets)
        self.assertFalse(self.buys())

    def test_switch_keeps_recovery_alongside_new_normal_market(self):
        self.fill("a-n", 5)
        self.bot.install_candidates([market("b")])
        self.cycles()
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertIn("a", self.bot.inventory_markets)
        self.assertEqual(set(self.buys()), {"a-y"})
        self.assertEqual(set(self.buys("b")), {"b-y", "b-n"})
        new_quotes = self.buys("b")
        self.fill("a-y", 5)
        self.cycles(5)
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertEqual(self.buys("b"), new_quotes)
        self.assertNotIn("a", self.bot.inventory_markets)
        self.assertFalse(self.buys())

    def test_red_stays_then_requires_two_healthy_snapshots(self):
        self.fill("a-n", 5)
        self.client.bid, self.client.ask = .40, .51
        self.bot.step()
        self.assertIn("a", self.bot.inventory_markets)
        self.assertFalse(self.buys())
        self.client.bid, self.client.ask = .40, .44
        self.bot.step()
        self.assertFalse(self.buys())
        self.bot.step()
        self.assertEqual(set(self.buys()), {"a-y"})

    def test_yellow_pauses_and_resumes(self):
        self.fill("a-n", 5)
        self.client.bid, self.client.ask = .45, .49
        self.bot.step()
        self.assertEqual(self.bot.inventory_health["a"].state, "YELLOW")
        self.assertFalse(self.buys())
        self.cycles()
        self.assertEqual(set(self.buys()), {"a-y"})

    def test_restart_restores_recovery_and_selects_normal_market(self):
        self.fill("a-n", 5)
        restarted = Bot(self.bot.config, self.client, InventoryTracker(self.path))
        restarted.initialize()
        restarted.install_candidates([market("b")])
        with patch("bot.restore_inventory_markets", return_value={"a": market("a")}):
            restarted.step()
        restarted.step()
        self.assertEqual(restarted.active_market.market_id, "b")
        self.assertIn("a", restarted.inventory_markets)
        self.assertEqual({o.token_id for o in restarted.orders.open_orders.values()}, {"a-y", "b-y", "b-n"})

    def test_closed_inventory_stays_without_blocking_normal_market(self):
        self.fill("a-n", 5)
        self.bot.inventory_markets["a"] = replace(self.bot.inventory_markets["a"], raw={"closed": True})
        self.bot.install_candidates([market("b")])
        with patch.object(self.client, "get_order_books", wraps=self.client.get_order_books) as fetch:
            self.cycles()
            self.assertTrue(all(call.args[0][0]["token_id"] == "b-y" for call in fetch.call_args_list))
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertEqual(set(self.buys("b")), {"b-y", "b-n"})
        self.assertFalse(self.buys())

    def test_unknown_cost_pauses_only_that_recovery_market(self):
        self.client.balances["a-y"] = 5
        self.bot.orders.dirty_inventory = True
        self.bot.install_candidates([market("b")])
        self.cycles()
        self.assertFalse(self.buys())
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertIn("a", self.bot.inventory_markets)
        self.assertEqual(set(self.buys("b")), {"b-y", "b-n"})

    def test_lagging_zero_balance_never_requotes(self):
        self.fill("a-y", 5)
        self.client.balances["a-y"] = 0
        self.bot.orders.dirty_inventory = True
        count = len(self.client.posts)
        self.cycles()
        self.assertEqual(len(self.client.posts), count)
        self.assertFalse(self.buys())

    def test_stop_priority_over_red_only_sells_unmatched_both_sides(self):
        for yes, no, token in ((10, 5, "a-y"), (5, 10, "a-n")):
            with self.subTest(token=token):
                self.client.bid, self.client.ask = .40, .44
                self.seed_position(yes, no, .60)
                self.client.bid, self.client.ask = .29, .30
                count = len(self.client.posts)
                self.bot.step()
                self.assertEqual(len(self.client.posts), count)
                self.bot.step()
                order = self.client.posts[-1]
                self.assertEqual(order.token_id, token)
                self.assertEqual(order.size, 5)
                from py_clob_client_v2 import Side
                self.assertEqual(order.side, Side.SELL)
                self.assertEqual(order.price, .29)
                self.bot.step()
                self.assertEqual(self.bot.inventory.positions["a"].yes, self.bot.inventory.positions["a"].no)
                self.assertNotIn("a", self.bot.inventory.stops)

    def test_partial_stop_retries_only_remaining_after_settlement(self):
        self.client.minimum_order_size = 1
        self.seed_position(10, 0, .60)
        self.client.stop_fill_size = 4
        self.client.bid, self.client.ask = .29, .33
        self.bot.step()
        self.bot.step()
        self.assertEqual(self.client.posts[-1].size, 10)
        self.bot.step()
        self.assertEqual(self.client.posts[-1].size, 6)
        self.assertEqual(self.bot.inventory.positions["a"].yes, 6)

    def test_pending_stop_trade_prevents_duplicate(self):
        self.seed_position(5, 0, .60)
        self.client.stop_trade_status = "MATCHED"
        self.client.bid, self.client.ask = .29, .33
        self.bot.step()
        self.bot.step()
        count = len(self.client.posts)
        self.cycles()
        self.assertEqual(len(self.client.posts), count)
        self.assertIn("a", self.bot.inventory.stops)

    def test_cancel_refusal_prevents_stop_submission(self):
        self.seed_position(5, 0, .60)
        self.client.refuse_cancel = True
        self.client.bid, self.client.ask = .29, .33
        count = len(self.client.posts)
        self.cycles()
        self.assertEqual(len(self.client.posts), count)
        self.assertTrue(self.bot.orders.finalizing)

    def test_stop_intent_survives_restart(self):
        self.seed_position(5, 0, .60)
        self.client.bid, self.client.ask = .29, .33
        self.bot.step()
        loaded = InventoryTracker(self.path)
        loaded.load_local()
        self.assertEqual(loaded.stops, {"a": "YES"})

    def test_confirmed_stop_with_lagging_balance_never_sells_twice(self):
        self.seed_position(5, 0, .60)
        self.client.bid, self.client.ask = .29, .33
        self.bot.step()
        self.bot.step()
        self.client.balances["a-y"] = 5  # Old replica after confirmed sell.
        count = len(self.client.posts)
        self.cycles()
        self.assertEqual(len(self.client.posts), count)
        self.assertIn("a", self.bot.inventory.stops)

    def test_stop_below_minimum_keeps_position_and_allows_normal_market(self):
        self.seed_position(1, 0, .60)
        self.client.bid, self.client.ask = .29, .33
        self.bot.install_candidates([market("b")])
        count = len(self.client.posts)
        self.cycles()
        self.assertFalse(self.buys())
        self.assertEqual(self.bot.active_market.market_id, "b")
        self.assertIn("a", self.bot.inventory_markets)
        self.assertIn("a", self.bot.inventory.stops)

    def test_resolution_cutoff_does_not_suppress_stop(self):
        from datetime import datetime, timedelta, timezone
        self.seed_position(5, 0, .60)
        self.bot.inventory_markets["a"] = replace(self.bot.inventory_markets["a"],
            end_date=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.client.bid, self.client.ask = .29, .33
        self.bot.step()
        self.bot.step()
        from py_clob_client_v2 import Side
        self.assertEqual(self.client.posts[-1].side, Side.SELL)

    def test_no_liquidity_does_not_submit_stop(self):
        self.bot.orders.cancel_market("condition-a")
        self.bot.orders.dirty_inventory = False
        empty = replace(book("a-y"), bids=())
        self.assertFalse(self.bot.orders.close_unmatched_position(market(), empty, 5))

    def test_failed_stop_persistence_prevents_execution(self):
        self.seed_position(5, 0, .60)
        self.client.bid, self.client.ask = .29, .33
        count = len(self.client.posts)
        with patch.object(self.bot.inventory, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bot.step()
        self.assertEqual(len(self.client.posts), count)
        self.assertNotIn("a", self.bot.inventory.stops)

    def test_cancel_fill_race_never_posts_stale_size(self):
        before = self.buys()
        self.client.fill(before["a-y"], 1)
        cancel = self.client.cancel_orders
        raced = False
        def racing_cancel(ids):
            nonlocal raced
            if not raced:
                self.client.fill(before["a-n"], 2)
                raced = True
            return cancel(ids)
        with patch.object(self.client, "cancel_orders", side_effect=racing_cancel):
            self.bot.step()
        self.cycles()
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.bot.inventory.positions["a"].yes, 1)
        self.assertEqual(self.bot.inventory.positions["a"].no, 2)
        self.assertFalse(self.buys())
