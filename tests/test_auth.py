import unittest
from unittest.mock import patch

import httpx
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.exceptions import PolyApiException

from auth import ResilientClobClient


class TestReadRecovery(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(ResilientClobClient)
        self.timeout = PolyApiException(error_msg="Request exception!")
        self.timeout.__context__ = httpx.ReadTimeout("read timed out")

    def test_timeout_retries_once_and_returns_fresh_response(self):
        with patch.object(ClobClient, "_get", side_effect=[self.timeout, {"balance": "5"}]) as get, \
             patch("auth.time.sleep"):
            self.assertEqual(self.client._get("/balance", params={"x": 1}), {"balance": "5"})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0], get.call_args_list[1])

    def test_persistent_timeout_is_bounded(self):
        with patch.object(ClobClient, "_get", side_effect=self.timeout) as get, \
             patch("auth.time.sleep"), self.assertRaises(PolyApiException):
            self.client._get("/balance")
        self.assertEqual(get.call_count, 2)

    def test_client_rejection_is_not_retried(self):
        error = PolyApiException(httpx.Response(401, json={"error": "unauthorized"}))
        with patch.object(ClobClient, "_get", side_effect=error) as get, \
             self.assertRaises(PolyApiException):
            self.client._get("/balance")
        self.assertEqual(get.call_count, 1)

    def test_write_paths_are_inherited_without_retry_override(self):
        self.assertIs(ResilientClobClient._post, ClobClient._post)
        self.assertIs(ResilientClobClient._delete, ClobClient._delete)
