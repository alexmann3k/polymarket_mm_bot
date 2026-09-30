import unittest
from unittest.mock import Mock, patch

from orders import OrderManager, OrderStateError
from helpers import config, market, FakeClob


class TestOrders(unittest.TestCase):
    def setUp(self):
        self.client = FakeClob()
        self.manager = OrderManager(self.client, config(dry_run=False))
        self.manager.dirty_inventory = False
        self.targets = {("a-y", "BUY"): (.41, .01)}
        self.manager.place_missing(market(), self.targets)

    def test_unchanged_quotes_do_not_post(self):
        self.manager.sync()
        self.manager.place_missing(market(), self.targets)
        self.assertEqual(len(self.client.posts), 1)

    def test_multiple_missing_quotes_use_one_batch_write(self):
        self.manager.place_missing(
            market("b"), {("b-y", "BUY"): (.41, .01), ("b-n", "BUY"): (.51, .01)})
        self.assertEqual(len(self.client.batch_posts), 1)
        self.assertEqual(len(self.client.batch_posts[0]), 2)
        self.assertEqual(len(self.client.posts), 3)

    def test_refused_cancel_keeps_tracking(self):
        self.client.refuse_cancel = True
        self.manager.cancel_market("condition-a")
        self.assertIn("1", self.manager.open_orders)
        self.assertIn("1", self.manager.finalizing)

    def test_market_cancel_leaves_other_market(self):
        self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.manager.cancel_market("condition-a")
        self.assertEqual({o.condition_id for o in self.manager.open_orders.values()}, {"condition-b"})

    def test_partial_fill_pending_blocks_replacement(self):
        self.client.fill("1", 2, "MATCHED")
        self.manager.sync()
        self.assertEqual(self.manager.open_orders["1"].remaining, 3)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets)

    def test_cancel_fill_race_retains_trade(self):
        self.client.fill("1", 2, "MINED")
        self.manager.cancel_market("condition-a")
        self.assertFalse(self.manager.open_orders)
        self.assertIn("trade-1", self.manager.pending_trades)
        self.assertFalse(self.manager.finish_settlements())
        self.client.trades["trade-1"]["status"] = "CONFIRMED"
        self.assertTrue(self.manager.finish_settlements())
        self.assertTrue(self.manager.dirty_inventory)

    def test_missing_order_is_reconciled_not_assumed_filled(self):
        self.client.rows["1"]["status"] = "CANCELED"
        self.manager.sync()
        self.assertFalse(self.manager.open_orders)
        self.assertFalse(self.manager.pending_trades)

    def test_inconsistent_open_order_responses_quarantine_trading(self):
        self.client.get_open_orders = Mock(return_value=[])
        # Detail endpoint still claims LIVE: neither filled nor cancelled is proven.
        self.manager.sync()
        self.assertIn("1", self.manager.open_orders)
        self.assertIn("1", self.manager.finalizing)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets)

    def test_order_detail_accepts_order_id_variants(self):
        self.client.rows["1"]["orderID"] = self.client.rows["1"].pop("id")
        self.client.get_open_orders = Mock(return_value=[])
        self.manager.sync()
        self.assertIn("1", self.manager.finalizing)

    def test_malformed_order_detail_remains_quarantined_until_timeout(self):
        self.client.get_open_orders = Mock(return_value=[])
        self.client.get_order = Mock(return_value={"unexpected": "response"})
        self.assertTrue(self.manager.sync())
        self.assertIn("1", self.manager.finalizing)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets)

    def test_recently_finalized_order_still_in_open_list_pauses_without_stopping(self):
        stale_row = dict(self.client.rows["1"])
        self.manager.cancel_market("condition-a")
        self.assertNotIn("1", self.manager.open_orders)
        self.client.get_open_orders = Mock(return_value=[stale_row])
        self.assertFalse(self.manager.sync())
        self.assertFalse(self.manager.blocked)
        self.assertIn("1", self.manager.recently_finalized)
        self.client.get_open_orders = Mock(return_value=[])
        self.assertTrue(self.manager.sync())
        self.assertNotIn("1", self.manager.recently_finalized)

    def test_acknowledged_cancel_with_stale_live_detail_stays_quarantined(self):
        original_get_order = self.client.get_order
        def stale_live(order_id):
            row = original_get_order(order_id)
            row["status"] = "LIVE"
            return row
        self.client.get_order = Mock(side_effect=stale_live)
        self.manager.cancel_market("condition-a")
        self.assertIn("1", self.manager.open_orders)
        self.assertIn("1", self.manager.finalizing)
        self.assertIn("1", self.manager.cancel_acknowledged)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets)
        self.client.get_order = original_get_order
        self.assertTrue(self.manager.finish_settlements())
        self.assertFalse(self.manager.open_orders)

    def test_unacknowledged_cancel_is_retried(self):
        self.client.refuse_cancel = True
        self.manager.cancel_market("condition-a")
        self.assertNotIn("1", self.manager.cancel_acknowledged)
        self.assertIn("1", self.manager.finalizing)
        self.client.refuse_cancel = False
        self.manager.cancel_market("condition-a")
        self.assertFalse(self.manager.open_orders)

    def test_acknowledged_cancel_is_not_sent_again_by_watchdog(self):
        original_get_order = self.client.get_order
        self.client.get_order = Mock(side_effect=lambda oid: {**original_get_order(oid), "status": "LIVE"})
        self.manager.cancel_market("condition-a")
        calls = len(self.client.cancels)
        self.manager.last_healthy -= self.manager.config.order_ttl_seconds
        self.manager.cancel_expired()
        self.assertEqual(len(self.client.cancels), calls)

    def test_fill_without_trade_ids_blocks(self):
        self.client.fill("1", 2)
        self.client.rows["1"]["associate_trades"] = []
        self.manager.sync()
        self.assertTrue(self.manager.finalizing)

    def test_fixed6_size_ratio(self):
        self.client.rows["1"].update(original_size="5000000", size_matched="2000000",
                                     associate_trades=["t"])
        self.client.trades["t"] = dict(id="t", status="CONFIRMED")
        self.manager.sync()
        self.assertEqual(self.manager.open_orders["1"].matched, 2)

    def test_ambiguous_post_is_fatal(self):
        self.client.post_order = Mock(side_effect=TimeoutError("lost response"))
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.assertTrue(self.manager.blocked)

    def test_malformed_post_is_fatal(self):
        self.client.post_order = Mock(return_value={"success": True})
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})

    def test_known_http_rejection_is_recoverable(self):
        class Rejected(Exception):
            status_code = 400
        self.client.post_order = Mock(side_effect=Rejected("post-only would cross"))
        with self.assertRaises(RuntimeError) as caught:
            self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.assertNotIsInstance(caught.exception, OrderStateError)
        self.assertFalse(self.manager.blocked)

    def test_cancel_without_fill_needs_no_balance_refresh(self):
        self.manager.cancel_market("condition-a")
        self.assertFalse(self.manager.dirty_inventory)

    def test_health_watchdog_cancels_stale_quotes(self):
        self.manager.last_healthy -= 1000
        self.manager.cancel_expired()
        self.assertFalse(self.manager.open_orders)

    def test_regular_refresh_cancels_only_active_market_after_interval(self):
        self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.assertFalse(self.manager.refresh_quotes("condition-a"))
        self.manager.open_orders["1"].created_at -= self.manager.config.quote_refresh_seconds
        self.assertTrue(self.manager.refresh_quotes("condition-a"))
        self.assertEqual({o.condition_id for o in self.manager.open_orders.values()}, {"condition-b"})

    def test_normal_refresh_still_uses_existing_timeout(self):
        from dataclasses import replace
        self.manager.config = replace(self.manager.config, quote_refresh_seconds=5)
        created = self.manager.open_orders["1"].created_at
        with patch("orders.time.monotonic", return_value=created + 6):
            self.assertTrue(self.manager.refresh_quotes("condition-a"))
        self.assertFalse(self.manager.open_orders)

    def test_recovery_gtd_uses_35_seconds_plus_exchange_buffer_without_age_cancel(self):
        self.assertEqual(self.client.order_types, ["GTC"])
        self.assertEqual(self.client.posts[0].expiration, 0)
        self.manager.cancel_market("condition-a")
        with patch("orders.time.time", return_value=1000):
            self.manager.place_missing(market(), self.targets, inventory_reduction=True)
        self.assertEqual(self.client.order_types[-1], "GTD")
        self.assertEqual(self.client.posts[-1].expiration, 1240)
        self.manager.open_orders["2"].created_at -= 1000
        self.assertFalse(self.manager.refresh_quotes("condition-a", inventory_reduction=True))
        self.assertIn("2", self.manager.open_orders)

    def test_expiry_reconciles_partial_fill_before_replacement(self):
        self.client.fill("1", 2, "MINED")
        self.client.rows["1"]["status"] = "EXPIRED"
        self.manager.sync()
        self.assertNotIn("1", self.manager.open_orders)
        self.assertIn("trade-1", self.manager.pending_trades)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets, inventory_reduction=True)

    def test_expired_timestamp_with_stale_live_response_quarantines(self):
        self.manager.open_orders["1"].expiration = 1
        self.manager.sync()
        self.assertIn("1", self.manager.finalizing)
        with self.assertRaises(OrderStateError):
            self.manager.place_missing(market(), self.targets, inventory_reduction=True)
        self.client.rows["1"]["status"] = "EXPIRED"
        self.manager.sync()
        self.assertNotIn("1", self.manager.open_orders)

    def test_gtd_rejection_does_not_fall_back_to_gtc(self):
        self.client.post_order = Mock(return_value={"success": False, "errorMsg": "invalid expiration"})
        with self.assertRaises(RuntimeError):
            self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)}, inventory_reduction=True)
        self.client.post_order.assert_called_once()
        self.assertEqual(self.client.post_order.call_args.args[1], "GTD")

    def test_recovery_batch_is_gtd(self):
        self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01),
                                                 ("b-n", "BUY"): (.41, .01)}, inventory_reduction=True)
        self.assertTrue(all(x.orderType == "GTD" and x.order.expiration > 0
                            for x in self.client.batch_posts[-1]))

    def test_recovery_lifetime_env_override_and_validation(self):
        from config import Config
        with patch.dict("os.environ", {"INVENTORY_QUOTE_EXPIRATION_SECONDS": "42.5"}, clear=True):
            self.manager.config = Config.from_env()
        self.assertEqual(self.manager.config.inventory_quote_expiration_seconds, 42.5)
        self.assertEqual(self.manager.config.quote_refresh_seconds, 15)
        with patch("orders.time.time", return_value=1000):
            self.assertEqual(self.manager.quote_expiration(True), 1240)
        for value in ("0", "-1", "nan", "inf"):
            with patch.dict("os.environ", {"INVENTORY_QUOTE_EXPIRATION_SECONDS": value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_health_watchdog_ignores_longer_recovery_lifetime(self):
        self.manager.open_orders["1"].expiration = 9999999999
        created = self.manager.open_orders["1"].created_at
        self.manager.last_healthy = created - self.manager.config.order_ttl_seconds
        with patch("orders.time.monotonic", return_value=created + 10):
            self.assertFalse(self.manager.refresh_quotes("condition-a", inventory_reduction=True))
            self.manager.cancel_expired()
        self.assertFalse(self.manager.open_orders)

    def test_obsolete_cancellation_is_scoped_to_market(self):
        self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.manager.cancel_obsolete({}, "condition-a")
        self.assertEqual({o.condition_id for o in self.manager.open_orders.values()}, {"condition-b"})

    def test_batch_missing_orders_cannot_exceed_account_slot_limit(self):
        from dataclasses import replace
        self.manager.config = replace(self.manager.config, max_open_orders=2)
        self.manager.place_missing(market("b"),
                                   {("b-y", "BUY"): (.41, .01), ("b-n", "BUY"): (.41, .01)})
        self.assertEqual(len(self.manager.open_orders), 2)

    def test_refresh_is_measured_from_placement_not_bot_start(self):
        self.manager.last_healthy -= 1_000
        self.assertFalse(self.manager.refresh_quotes("condition-a"))
        self.assertIn("1", self.manager.open_orders)

    def test_refresh_fill_race_marks_inventory_dirty(self):
        self.client.fill("1", 2, "CONFIRMED")
        self.manager.open_orders["1"].created_at -= self.manager.config.quote_refresh_seconds
        self.assertTrue(self.manager.refresh_quotes("condition-a"))
        self.assertTrue(self.manager.dirty_inventory)

    def test_heartbeat_failure_prevents_new_order(self):
        self.manager.last_heartbeat = float("-inf")
        self.client.post_heartbeat = Mock(side_effect=RuntimeError("outage"))
        with self.assertRaises(RuntimeError):
            self.manager.place_missing(market("b"), {("b-y", "BUY"): (.41, .01)})
        self.assertEqual(len(self.client.posts), 1)

    def test_startup_cancels_previous_run(self):
        new = OrderManager(self.client, config(dry_run=False))
        new.startup()
        self.assertFalse(self.client.get_open_orders())

    def test_dry_run_has_no_write_requests(self):
        client = Mock()
        manager = OrderManager(client, config())
        manager.dirty_inventory = False
        manager.startup()
        manager.place_missing(market(), self.targets)
        manager.cancel_all()
        self.assertEqual(client.mock_calls, [])
