import unittest
from decimal import ROUND_CEILING

from fair_value import compute_fair_value
from quoting import quote_book, round_to_tick
from orderbook import get_pair_orderbooks, without_own_orders
from orders import TrackedOrder
from helpers import book, config, FakeClob


class TestModels(unittest.TestCase):
    def test_mid(self):
        self.assertAlmostEqual(compute_fair_value(book(bid=.40, ask=.44)), .42)

    def test_tick_rounding(self):
        self.assertEqual(round_to_tick(.423, .01), .42)
        self.assertEqual(round_to_tick(.445, .01, ROUND_CEILING), .45)

    def test_requested_quote_example(self):
        self.assertEqual(quote_book(book(), config()), (.41, .43))

    def test_one_tick_spread_has_no_quote(self):
        self.assertIsNone(quote_book(book(bid=.44, ask=.45), config()))

    def test_two_tick_spread_also_has_no_edge(self):
        # Bid and ask would both be .45, so the bot must skip this book.
        self.assertIsNone(quote_book(book(bid=.44, ask=.46), config()))

    def test_small_tick(self):
        quotes = quote_book(book(bid=.401, ask=.409, tick=.001),
                            config(min_quote_spread=.002))
        self.assertEqual(quotes, (.402, .408))

    def test_market_minimum_above_five(self):
        self.assertIsNone(quote_book(book(minimum=10), config()))

    def test_no_edge(self):
        self.assertIsNone(quote_book(book(), config(min_quote_spread=.03)))

    def test_batch_books_resolved_by_token_not_response_order(self):
        client = FakeClob()
        yes, no = get_pair_orderbooks(client, "y", "n")
        self.assertEqual((yes.token_id, no.token_id), ("y", "n"))
        self.assertEqual(client.book_calls, 1)

    def test_subtract_own_size_not_entire_shared_level(self):
        tracked = TrackedOrder("o", "y", "BUY", .40, 5, 0)
        external = without_own_orders(book(), [tracked])
        self.assertEqual(external.bids[0].size, 15)

    def test_invalid_config(self):
        for changes in (dict(order_size=6), dict(poll_interval_seconds=0),
                        dict(movement_threshold=float("nan")), dict(percentile_low=80),
                        dict(yellow_snapshots=0)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_invalid_dry_run_value_cannot_enable_live_trading(self):
        import os
        from unittest.mock import patch
        from config import Config
        with patch.dict(os.environ, {"DRY_RUN": "truue"}, clear=True), self.assertRaises(ValueError):
            Config.from_env()
