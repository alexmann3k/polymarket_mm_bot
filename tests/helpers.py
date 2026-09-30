import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from config import Config
from models import Level, OrderBook, MarketCandidate


def config(**changes):
    with patch.dict(os.environ, {}, clear=True):
        return replace(Config.from_env(), **changes)


def book(token="y", bid=.40, ask=.44, tick=.01, minimum=5):
    return OrderBook(token, (Level(bid, 20),), (Level(ask, 20),), tick, minimum, 0)


def market(mid="a"):
    return MarketCandidate(mid, "condition-" + mid, "Question " + mid,
                           mid + "-y", mid + "-n", datetime.now(timezone.utc) + timedelta(days=2),
                           10000, .04)


class FakeClob:
    """In-memory exchange. No network, signing, credentials, or real CSV."""
    def __init__(self):
        self.rows = {}
        self.posts = []
        self.order_types = []
        self.batch_posts = []
        self.cancels = []
        self.trades = {}
        self.balances = {}
        self.book_calls = self.balance_calls = self.open_calls = 0
        self.bid, self.ask = .40, .44
        self.refuse_cancel = False
        self.fail_balance = False
        self.minimum_order_size = 5
        self.stop_fill_size = None
        self.stop_trade_status = "CONFIRMED"

    def post_heartbeat(self, heartbeat_id=""):
        return {"heartbeat_id": "test-heartbeat"}

    def create_order(self, args, options):
        return args

    def post_order(self, order, order_type, post_only=False):
        from py_clob_client_v2 import Side, OrderType
        assert post_only or order_type == OrderType.FAK
        oid = str(len(self.posts) + 1)
        self.posts.append(order)
        self.order_types.append(order_type)
        self.rows[oid] = dict(id=oid, asset_id=order.token_id,
            side="BUY" if order.side == Side.BUY else "SELL", price=str(order.price),
            original_size=str(order.size), size_matched="0", status="LIVE", associate_trades=[])
        if order_type == OrderType.FAK:
            amount = order.size if self.stop_fill_size is None else min(order.size, self.stop_fill_size)
            if amount:
                self.fill(oid, amount, self.stop_trade_status)
            self.rows[oid]["status"] = "MATCHED" if amount == order.size else "CANCELED"
            return {"success": True, "orderID": oid, "status": "matched" if amount else "unmatched"}
        return {"success": True, "orderID": oid, "status": "live"}

    def post_orders(self, args, post_only=False):
        self.batch_posts.append(args)
        responses = []
        for arg in args:
            responses.append(self.post_order(arg.order, arg.orderType, post_only=post_only))
        return responses

    def get_open_orders(self):
        self.open_calls += 1
        return [dict(r) for r in self.rows.values() if r["status"] == "LIVE"]

    def get_order(self, oid):
        return dict(self.rows[oid])

    def cancel_orders(self, ids):
        ids = list(ids)
        self.cancels.append(ids)
        canceled = []
        for oid in ids:
            if not self.refuse_cancel and self.rows[oid]["status"] == "LIVE":
                self.rows[oid]["status"] = "CANCELED"
                canceled.append(oid)
        return {"canceled": canceled, "not_canceled": {i: "refused" for i in ids if i not in canceled}}

    def cancel_all(self):
        return self.cancel_orders([oid for oid, r in self.rows.items() if r["status"] == "LIVE"])

    def get_trades(self, params=None):
        if params and params.id:
            return [self.trades[params.id]] if params.id in self.trades else []
        return list(self.trades.values())

    def get_balance_allowance(self, params):
        self.balance_calls += 1
        if self.fail_balance:
            raise RuntimeError("simulated balance outage")
        value = self.balances.get(params.token_id, 100 if params.token_id is None else 5)
        return {"balance": str(int(value * 1_000_000))}

    def get_order_books(self, params):
        self.book_calls += 1
        result = []
        for p in params:
            token = p["token_id"]
            bids, asks = {self.bid: 20}, {self.ask: 20}
            for row in self.rows.values():
                if row["asset_id"] == token and row["status"] == "LIVE":
                    levels = bids if row["side"] == "BUY" else asks
                    price = float(row["price"])
                    levels[price] = levels.get(price, 0) + float(row["original_size"]) - float(row["size_matched"])
            result.append(dict(asset_id=token, tick_size=".01", min_order_size=str(self.minimum_order_size),
                bids=[dict(price=str(p), size=str(s)) for p, s in bids.items()],
                asks=[dict(price=str(p), size=str(s)) for p, s in asks.items()]))
        return list(reversed(result))

    def fill(self, oid, amount, status="CONFIRMED"):
        row = self.rows[oid]
        delta = amount - float(row["size_matched"])
        row["size_matched"] = str(amount)
        if amount == float(row["original_size"]):
            row["status"] = "MATCHED"
        tid = "trade-" + oid + ("-" + str(len(row["associate_trades"])) if row["associate_trades"] else "")
        row["associate_trades"].append(tid)
        self.trades[tid] = dict(id=tid, status=status, trader_side="TAKER", asset_id=row["asset_id"],
                               side=row["side"], size=str(delta), price=row["price"], match_time=len(self.trades))
        if status == "CONFIRMED":
            sign = 1 if row["side"] == "BUY" else -1
            token = row["asset_id"]
            self.balances[token] = self.balances.get(token, 5) + sign * delta
