import threading
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from scanner import fetch_markets_df, filter_markets, rank_markets, restore_token_ids, restore_inventory_markets
from helpers import config, market


def row(index, volume, spread=.04, liquidity=5000):
    m = market(str(index))
    return dict(market_id=m.market_id, condition_id=m.condition_id, question=m.question,
                yes_token_id=m.yes_token_id, no_token_id=m.no_token_id,
                yes_price=.43, no_price=.57, end_date=m.end_date, hours_to_resolution=48,
                category=None, accepting_orders=True, spread=spread, volume_24h=volume,
                liquidity=liquidity, volume=volume, raw={})


class TestScanner(unittest.TestCase):
    def test_empty_scan(self):
        self.assertEqual(rank_markets(pd.DataFrame(), config()), [])

    def test_absolute_activity_and_liquidity_limits(self):
        df = pd.DataFrame([row(1, 1), row(2, 1_000_000), row(3, 5000),
                           row(4, 4000, spread=.001), row(5, 4000, liquidity=1)])
        self.assertEqual([m.market_id for m in rank_markets(df, config())], ["3"])

    def test_middle_volume_band(self):
        df = pd.DataFrame([row(i, (i + 1) * 1000) for i in range(10)])
        kept = filter_markets(df, config())
        self.assertGreaterEqual(kept.volume_24h.min(), 4000)
        self.assertLessEqual(kept.volume_24h.max(), 8000)

    def test_sorted_candidates(self):
        df = pd.DataFrame([row(1, 5000, .02, 2000), row(2, 5000, .04, 8000)])
        candidates = rank_markets(df, config())
        self.assertEqual([m.market_id for m in candidates], ["2", "1"])
        self.assertGreater(candidates[0].score, candidates[1].score)

    def test_exact_request_limit_and_session_closed(self):
        session = Mock()
        response1, response2 = Mock(), Mock()
        response1.json.return_value = [{}] * 100
        response2.json.return_value = [{}] * 5
        session.get.side_effect = [response1, response2]
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=False)
        with patch("scanner.requests.Session", return_value=context):
            fetch_markets_df(config(scanner_request_limit=105))
        self.assertEqual([c.kwargs["params"]["limit"] for c in session.get.call_args_list], [100, 5])
        context.__exit__.assert_called_once()

    def test_bad_api_shape_raises(self):
        with patch("scanner.requests.Session") as factory:
            factory.return_value.__enter__.return_value.get.return_value.json.return_value = {}
            with self.assertRaises(ValueError):
                fetch_markets_df(config())

    def test_stop_scan_between_pages(self):
        event = threading.Event()
        event.set()
        with patch("scanner.requests.Session") as factory:
            self.assertTrue(fetch_markets_df(config(), event).empty)
            factory.return_value.__enter__.return_value.get.assert_not_called()

    def test_legacy_holdings_use_single_market_endpoints(self):
        session = Mock()
        first, second = Mock(), Mock()
        first.status_code, second.status_code = 200, 200
        first.json.return_value = {"id": "1", "outcomes": '["Yes", "No"]',
                                   "clobTokenIds": '["yes-1", "no-1"]'}
        second.json.return_value = {"id": "2", "outcomes": '["Yes", "No"]',
                                    "clobTokenIds": '["yes-2", "no-2"]'}
        session.get.side_effect = [first, second]
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=False)
        with patch("scanner.requests.Session", return_value=context):
            restored = restore_token_ids(config(), ["1", "2"])
        self.assertEqual(restored, {"1": ("yes-1", "no-1"), "2": ("yes-2", "no-2")})
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in session.get.call_args_list], ["1", "2"])

    def test_unresolvable_legacy_holding_reports_exact_market_id(self):
        session = Mock()
        response = Mock(status_code=404)
        session.get.return_value = response
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=False)
        with patch("scanner.requests.Session", return_value=context), \
             self.assertRaisesRegex(ValueError, "999"):
            restore_token_ids(config(), ["999"])

    def test_inventory_metadata_restored_even_for_closed_market(self):
        with patch("scanner.requests.Session") as factory:
            response = factory.return_value.__enter__.return_value.get.return_value
            response.status_code = 200
            response.json.return_value = {
                "id": "old", "conditionId": "condition-old", "closed": True,
                "outcomes": '["No", "Yes"]', "clobTokenIds": '["n", "y"]',
                "endDate": "2025-01-01T00:00:00Z"}
            restored = restore_inventory_markets(config(), ["old"])["old"]
        self.assertEqual((restored.yes_token_id, restored.no_token_id), ("y", "n"))
        self.assertEqual(restored.condition_id, "condition-old")
        self.assertEqual(restored.end_date.year, 2025)

    def test_missing_lifecycle_metadata_fails_closed(self):
        with patch("scanner.requests.Session") as factory:
            response = factory.return_value.__enter__.return_value.get.return_value
            response.status_code = 200
            response.json.return_value = {"outcomes": '["Yes", "No"]',
                                          "clobTokenIds": '["y", "n"]'}
            with self.assertRaisesRegex(ValueError, "missing lifecycle metadata for old"):
                restore_inventory_markets(config(), ["old"])
