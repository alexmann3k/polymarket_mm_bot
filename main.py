from __future__ import annotations

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from auth import build_client
from bot import Bot
from config import Config
from orders import OrderStateError
from scanner import fetch_markets_df, rank_markets

logger = logging.getLogger(__name__)

_SHUTDOWN_CANCEL_ATTEMPTS = 3
_SHUTDOWN_RETRY_DELAY_SECONDS = 1.0
GEOBLOCK_ENDPOINT = "https://polymarket.com/api/geoblock"
GEOBLOCK_TIMEOUT_SECONDS = 10.0


class TradingEligibilityError(RuntimeError):
    """Base error for a failed or denied geographic eligibility check."""


class TradingBlockedError(TradingEligibilityError):
    """The geoblock endpoint confirmed that trading is unavailable."""


class TradingEligibilityCheckError(TradingEligibilityError):
    """The geoblock response could not establish trading eligibility."""


def check_trading_eligibility() -> None:
    """Fail closed unless Polymarket confirms that this location is eligible."""
    try:
        response = requests.get(GEOBLOCK_ENDPOINT, timeout=GEOBLOCK_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise TradingEligibilityCheckError(
            "Unable to verify Polymarket trading eligibility because the geoblock "
            "check failed. Trading is disabled until the location can be verified."
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("blocked"), bool):
        raise TradingEligibilityCheckError(
            "Unable to verify Polymarket trading eligibility because the geoblock "
            "response was malformed. Trading is disabled until the location can be verified."
        )

    if payload["blocked"]:
        location = ", ".join(
            str(value) for value in (payload.get("country"), payload.get("region"))
            if value
        ) or "unknown location"
        raise TradingBlockedError(
            f"Polymarket trading is unavailable from the detected location ({location})."
        )


def configure_logging(config: Any) -> None:
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    config = Config.from_env()
    configure_logging(config)
    if config.dry_run:
        logger.info("Dry-run mode enabled: skipping geographic trading eligibility check.")
    else:
        try:
            check_trading_eligibility()
        except TradingEligibilityError as exc:
            logger.error("%s", exc)
            return

    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    bot = Bot(config, build_client(config))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gamma-scan")
    scan = None
    next_scan = 0.0
    last_scan_attempt = float("-inf")
    errors = scan_errors = 0

    def discover():
        return rank_markets(fetch_markets_df(config, stop_event), config)

    try:
        bot.initialize()
        logger.info("Starting V1 dry_run=%s scan_interval=%.0fs size=5",
                    config.dry_run, config.market_scan_interval_seconds)
        while not stop_event.is_set():
            now = time.monotonic()
            if scan is not None and scan.done():
                try:
                    bot.install_candidates(scan.result())
                    scan_errors = 0
                    next_scan = now + config.market_scan_interval_seconds
                except Exception:
                    scan_errors += 1
                    logger.exception("Market scan failed (%d/%d)", scan_errors, config.max_consecutive_errors)
                    next_scan = now + config.scan_retry_seconds
                    if scan_errors >= config.max_consecutive_errors:
                        raise RuntimeError("Repeated market scan failures")
                scan = None
            exhausted = bot.market is None and not bot.ranked_markets
            if (scan is None and (now >= next_scan or exhausted)
                    and now - last_scan_attempt >= config.scan_retry_seconds):
                last_scan_attempt = now
                scan = executor.submit(discover)
            try:
                bot.step()
                errors = 0
            except OrderStateError:
                raise
            except Exception:
                errors += 1
                logger.exception("Trading cycle failed (%d/%d); cancelling quotes",
                                 errors, config.max_consecutive_errors)
                try:
                    bot.orders.cancel_ids(bot.orders.open_orders)
                except Exception:
                    logger.exception("Cleanup not yet confirmed; no replacement orders")
                bot.orders.dirty_inventory = True
                if errors >= config.max_consecutive_errors:
                    raise RuntimeError("Repeated trading failures; stopping")
            bot.report_status()
            stop_event.wait(config.poll_interval_seconds)
    finally:
        stop_event.set()
        # Cancel before waiting for the scanner thread.
        cancelled = False
        for attempt in range(_SHUTDOWN_CANCEL_ATTEMPTS):
            try:
                bot.orders.cancel_all()
                cancelled = True
                break
            except Exception:
                logger.exception("Shutdown cancellation attempt %d/%d failed",
                                 attempt + 1, _SHUTDOWN_CANCEL_ATTEMPTS)
                time.sleep(_SHUTDOWN_RETRY_DELAY_SECONDS)
        executor.shutdown(wait=True, cancel_futures=True)
        if not cancelled:
            raise RuntimeError("Shutdown could not confirm cancellation; inspect account orders")
        logger.info("Bot stopped; open orders cancelled, inventory retained")


if __name__ == "__main__":
    main()
