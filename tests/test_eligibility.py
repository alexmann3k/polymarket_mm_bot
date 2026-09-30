import unittest
from unittest.mock import Mock, patch

import requests

import main


class TestTradingEligibility(unittest.TestCase):
    def response(self, payload):
        response = Mock()
        response.json.return_value = payload
        return response

    def test_allowed_location(self):
        response = self.response({"blocked": False, "country": "DE", "region": "BE"})
        with patch("main.requests.get", return_value=response) as get:
            self.assertIsNone(main.check_trading_eligibility())

        get.assert_called_once_with(main.GEOBLOCK_ENDPOINT, timeout=main.GEOBLOCK_TIMEOUT_SECONDS)
        response.raise_for_status.assert_called_once_with()

    def test_blocked_location_includes_country_and_region(self):
        response = self.response({"blocked": True, "country": "DE", "region": "BE"})
        with patch("main.requests.get", return_value=response):
            with self.assertRaisesRegex(
                main.TradingBlockedError,
                r"Polymarket trading is unavailable from the detected location \(DE, BE\)\.",
            ):
                main.check_trading_eligibility()

    def test_malformed_response_fails_closed(self):
        response = self.response({"country": "DE"})
        with patch("main.requests.get", return_value=response):
            with self.assertRaisesRegex(
                main.TradingEligibilityCheckError,
                "response was malformed",
            ):
                main.check_trading_eligibility()

    def test_endpoint_failure_fails_closed_without_being_a_block(self):
        with patch(
            "main.requests.get",
            side_effect=requests.RequestException("network unavailable"),
        ):
            with self.assertRaisesRegex(
                main.TradingEligibilityCheckError,
                "geoblock check failed",
            ) as raised:
                main.check_trading_eligibility()

        self.assertNotIsInstance(raised.exception, main.TradingBlockedError)
