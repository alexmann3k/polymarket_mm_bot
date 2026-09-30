import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from helpers import config
from orders import OrderStateError


class TestRunner(unittest.TestCase):
    def run_bot(self, bot):
        cfg = config(poll_interval_seconds=.001, scan_retry_seconds=.001, max_consecutive_errors=2)
        with patch("main.Config.from_env", return_value=cfg), \
             patch("main.check_trading_eligibility"), \
             patch("main.build_client"), patch("main.Bot", return_value=bot), \
             patch("main.signal.signal"), patch("main.configure_logging"), \
             patch("main.fetch_markets_df"), patch("main.rank_markets", return_value=[]), \
             patch("main.time.sleep"):
            main.main()

    def bot(self):
        return SimpleNamespace(initialize=Mock(), step=Mock(), install_candidates=Mock(),
                               market=None, ranked_markets=[], orders=Mock(), report_status=Mock())

    def test_repeated_cycle_errors_stop_and_cleanup(self):
        bot = self.bot()
        bot.step.side_effect = RuntimeError("read outage")
        with self.assertLogs("main", "ERROR"), self.assertRaisesRegex(RuntimeError, "Repeated trading"):
            self.run_bot(bot)
        self.assertEqual(bot.step.call_count, 2)
        bot.orders.cancel_all.assert_called_once()

    def test_uncertain_write_stops_immediately_and_cleans_up(self):
        bot = self.bot()
        bot.step.side_effect = OrderStateError("unknown write result")
        with self.assertRaises(OrderStateError):
            self.run_bot(bot)
        self.assertEqual(bot.step.call_count, 1)
        bot.orders.cancel_all.assert_called_once()

    def test_startup_failure_also_cleans_up(self):
        bot = self.bot()
        bot.initialize.side_effect = RuntimeError("startup")
        with self.assertRaisesRegex(RuntimeError, "startup"):
            self.run_bot(bot)
        bot.orders.cancel_all.assert_called_once()
        bot.step.assert_not_called()

    def test_shutdown_retries_are_bounded_and_failure_is_visible(self):
        bot = self.bot()
        bot.initialize.side_effect = RuntimeError("startup")
        bot.orders.cancel_all.side_effect = RuntimeError("cancel outage")
        with self.assertLogs("main", "ERROR"), self.assertRaisesRegex(RuntimeError, "could not confirm"):
            self.run_bot(bot)
        self.assertEqual(bot.orders.cancel_all.call_count, 3)

    def test_blocked_location_stops_before_client_or_bot_startup(self):
        cfg = config(dry_run=False)
        with patch("main.Config.from_env", return_value=cfg), \
             patch("main.configure_logging"), \
             patch(
                 "main.check_trading_eligibility",
                 side_effect=main.TradingBlockedError(
                     "Polymarket trading is unavailable from the detected location (DE)."
                 ),
             ) as check, \
             patch("main.build_client") as build, \
             patch("main.Bot") as bot:
            with self.assertLogs("main", "ERROR") as logs:
                main.main()

        check.assert_called_once_with()
        build.assert_not_called()
        bot.assert_not_called()
        self.assertIn("detected location (DE)", logs.output[0])

    def test_live_allowed_location_continues_startup(self):
        cfg = config(dry_run=False)
        bot = self.bot()
        bot.step.side_effect = OrderStateError("stop after startup assertion")
        client = Mock()
        with patch("main.Config.from_env", return_value=cfg), \
             patch("main.configure_logging"), \
             patch("main.check_trading_eligibility") as check, \
             patch("main.build_client", return_value=client) as build, \
             patch("main.Bot", return_value=bot) as bot_factory, \
             patch("main.signal.signal"), \
             patch("main.fetch_markets_df"), \
             patch("main.rank_markets", return_value=[]), \
             patch("main.time.sleep"):
            with self.assertRaises(OrderStateError):
                main.main()

        check.assert_called_once_with()
        build.assert_called_once_with(cfg)
        bot_factory.assert_called_once_with(cfg, client)
        bot.initialize.assert_called_once_with()

    def test_dry_run_skips_geoblock_and_does_not_place_orders(self):
        cfg = config(dry_run=True)
        bot = self.bot()
        bot.step.side_effect = OrderStateError("stop after startup assertion")
        client = Mock()
        with patch("main.Config.from_env", return_value=cfg), \
             patch("main.configure_logging"), \
             patch(
                 "main.check_trading_eligibility",
                 side_effect=main.TradingBlockedError(
                     "Polymarket trading is unavailable from the detected location (DE)."
                 ),
             ) as check, \
             patch("main.build_client", return_value=client), \
             patch("main.Bot", return_value=bot), \
             patch("main.signal.signal"), \
             patch("main.fetch_markets_df"), \
             patch("main.rank_markets", return_value=[]), \
             patch("main.time.sleep"):
            with self.assertLogs("main", "INFO") as logs:
                with self.assertRaises(OrderStateError):
                    main.main()

        check.assert_not_called()
        self.assertIn(
            "Dry-run mode enabled: skipping geographic trading eligibility check.",
            "\n".join(logs.output),
        )
        client.post_order.assert_not_called()
        client.post_orders.assert_not_called()
