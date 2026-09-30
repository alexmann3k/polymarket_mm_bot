from __future__ import annotations

import argparse
import logging
from types import SimpleNamespace

from auth import bootstrap_client
from config import Config
from orderbook import get_orderbook
from orders import OrderManager
from quoting import round_to_tick


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--place-test-order", action="store_true")
    parser.add_argument("--token-id", default="")
    args = parser.parse_args()
    config = Config.from_env()
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    client = bootstrap_client(config)
    print("Authenticated CLOB client OK")

    token_id = args.token_id or config.token_id
    if not token_id:
        print("No TOKEN_ID supplied; auth-only sanity check complete.")
        return
    book = get_orderbook(client, token_id)
    print(f"Orderbook OK: bid={book.best_bid} ask={book.best_ask} tick={book.tick_size}")
    if not args.place_test_order:
        print("No order placed. Use --place-test-order explicitly.")
        return
    if config.dry_run:
        raise SystemExit("DRY_RUN=true: explicit test order requires DRY_RUN=false.")
    price = config.sanity_test_price
    if (book.best_ask is None or price >= book.best_ask or book.minimum_order_size > 5
            or not book.tick_size <= price <= 1 - book.tick_size
            or abs(round_to_tick(price, book.tick_size) - price) > 1e-9):
        raise SystemExit("Test price/size invalid for this book; no order placed.")
    orders = OrderManager(client, config)
    try:
        orders.startup()
        if not orders.finish_settlements():
            raise RuntimeError("Previous trades still settling; no test order placed.")
        orders.dirty_inventory = False
        orders.place_missing(SimpleNamespace(condition_id="sanity"),
                             {(token_id, "BUY"): (price, book.tick_size)})
        print("Post-only test order placed; cancelling immediately.")
    finally:
        orders.cancel_all()


if __name__ == "__main__":
    main()
