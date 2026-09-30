from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from models import MarketCandidate

logger = logging.getLogger(__name__)

_GAMMA_PAGE_SIZE = 100
_GAMMA_REQUEST_TIMEOUT_SECONDS = 15
_RANK_LOG_LIMIT = 5


def _parse_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _float_or_nan(value: Any) -> float:
    try:
        if value is None or value == "":
            return float("nan")
        number = float(value)
        return number if math.isfinite(number) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _extract_yes_no(market: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return YES token, NO token, YES price, NO price from aligned Gamma arrays."""
    outcomes = _parse_list(market.get("outcomes"))
    token_ids = _parse_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
    prices = _parse_list(market.get("outcomePrices") or market.get("outcome_prices"))

    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    mapping: dict[str, tuple[str, str]] = {}
    for idx, outcome in enumerate(outcomes):
        if idx >= len(prices):
            price = "nan"
        else:
            price = str(prices[idx])
        mapping[str(outcome).strip().lower()] = (str(token_ids[idx]), price)

    yes = mapping.get("yes")
    no = mapping.get("no")
    if yes is None or no is None:
        return None
    return yes[0], no[0], yes[1], no[1]


def _market_request_params(config: Any, limit: int, offset: int) -> dict[str, Any]:
    """Build one Gamma page request without changing the discovery constraints."""
    return {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
        "order": "volume24hr",
        "ascending": "false",
        "liquidity_num_min": config.min_liquidity,
        "end_date_min": (
            datetime.now(timezone.utc) + timedelta(hours=config.min_resolution_hours)
        ).isoformat(),
    }


def _fetch_market_pages(config: Any, stop_event: Any = None) -> tuple[list[Any], int] | None:
    """Fetch bounded Gamma pages, returning ``None`` when cancellation is requested."""
    all_markets: list[Any] = []
    offset = 0
    requests_made = 0
    with requests.Session() as session:
        while len(all_markets) < config.scanner_request_limit:
            if stop_event is not None and stop_event.is_set():
                return None
            limit = min(_GAMMA_PAGE_SIZE, config.scanner_request_limit - len(all_markets))
            response = session.get(
                f"{config.gamma_api_url.rstrip('/')}/markets",
                params=_market_request_params(config, limit, offset),
                timeout=_GAMMA_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            requests_made += 1
            page = response.json()
            if not isinstance(page, list):
                raise ValueError("Gamma /markets response was not a list")
            if not page:
                break
            all_markets.extend(page[:limit])
            if len(page) < limit:
                break
            offset += len(page)
    return all_markets, requests_made


def _discovery_row(market: Any, now: datetime) -> dict[str, Any] | None:
    """Normalize one valid Gamma market into the scanner's DataFrame schema."""
    if not isinstance(market, dict):
        return None
    if not market.get("active", False) or market.get("closed", True):
        return None
    if not market.get("enableOrderBook", market.get("enable_order_book", False)):
        return None

    end_date = _parse_datetime(market.get("endDate") or market.get("end_date_iso"))
    if end_date is None:
        return None

    token_data = _extract_yes_no(market)
    if token_data is None:
        return None
    yes_token, no_token, yes_price, no_price = token_data
    if not market.get("id") or not market.get("conditionId") or not yes_token or yes_token == no_token:
        return None

    # Gamma already provides discovery-time market data. Prefer numeric helper
    # fields when present, otherwise fall back to their string forms.
    return {
        "market_id": str(market.get("id", "")),
        "condition_id": str(market.get("conditionId", "")),
        "question": str(market.get("question", "")),
        "slug": market.get("slug"),
        "category": market.get("category"),
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "yes_price": _float_or_nan(yes_price),
        "no_price": _float_or_nan(no_price),
        "best_bid": _float_or_nan(market.get("bestBid")),
        "best_ask": _float_or_nan(market.get("bestAsk")),
        "spread": _float_or_nan(market.get("spread")),
        "volume_24h": _float_or_nan(market.get("volume24hr", market.get("volume24hrClob"))),
        "volume": _float_or_nan(market.get("volumeNum", market.get("volume"))),
        "liquidity": _float_or_nan(market.get("liquidityNum", market.get("liquidity"))),
        "end_date": end_date,
        "hours_to_resolution": (end_date - now).total_seconds() / 3600.0,
        "accepting_orders": market.get("acceptingOrders") is True,
        "raw": market,
    }


def fetch_markets_df(config: Any, stop_event: Any = None) -> pd.DataFrame:
    """Fetch and normalize currently listed Gamma markets for local filtering."""
    fetched = _fetch_market_pages(config, stop_event)
    if fetched is None:
        return pd.DataFrame()
    raw_markets, requests_made = fetched
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for market in raw_markets:
        row = _discovery_row(market, now)
        if row is not None:
            rows.append(row)

    df = pd.DataFrame(rows)
    logger.info("Market discovery: %d Gamma requests, %d usable markets", requests_made, len(df))
    return df


def _exclude_sports(df: pd.DataFrame) -> pd.DataFrame:
    """Exclude the sports category before applying numeric market filters."""
    return df[df["category"].fillna("").str.lower() != "sports"]


def _apply_absolute_filters(df: pd.DataFrame, config: Any) -> pd.DataFrame:
    """Apply the configured activity, price, spread, and liquidity bounds."""
    return df[
        df["accepting_orders"].eq(True)
        & df["hours_to_resolution"].ge(float(config.min_resolution_hours))
        & df["yes_price"].between(float(config.min_price), float(config.max_price))
        & df["no_price"].between(float(config.min_price), float(config.max_price))
        & df["spread"].between(float(config.min_spread), float(config.max_spread))
        & df["volume_24h"].between(config.min_volume_24h, config.max_volume_24h)
        & df["liquidity"].ge(config.min_liquidity)
    ].copy()


def _apply_volume_band(df: pd.DataFrame, config: Any) -> pd.DataFrame:
    """Keep the configured middle volume percentile band and one row per condition."""
    if df.empty:
        return df
    low, high = df["volume_24h"].quantile(
        [config.percentile_low / 100, config.percentile_high / 100], interpolation="nearest")
    return df[df["volume_24h"].between(low, high)].drop_duplicates("condition_id").copy()


def filter_markets(df: pd.DataFrame, config: Any) -> pd.DataFrame:
    """Apply local market filters without making network requests."""
    if df.empty:
        return df.copy()
    return _apply_volume_band(_apply_absolute_filters(_exclude_sports(df), config), config)


def _score_markets(df: pd.DataFrame) -> pd.DataFrame:
    """Score filtered markets using the existing equal spread/liquidity ranks."""
    result = df.copy()
    result["score"] = (result["spread"].rank(pct=True) + result["liquidity"].rank(pct=True)) / 2
    return result


def rank_markets(df: pd.DataFrame, config: Any) -> list[MarketCandidate]:
    """Filter, score, sort, and expose market candidates in priority order."""
    filtered = filter_markets(df, config)
    if filtered.empty:
        return []
    # Equal, transparent weights: spread and liquidity ranks after activity filtering.
    result = _score_markets(filtered)
    result = result.sort_values(["score", "volume_24h", "market_id"], ascending=[False, True, True])
    candidates = [_candidate(row) for _, row in result.iterrows()]
    for index, market in enumerate(candidates[:_RANK_LOG_LIMIT], 1):
        logger.info("Rank %d score=%.3f market=%s spread=%.4f %s", index, market.score,
                    market.market_id, market.spread, market.question)
    return candidates


def select_best_market(df: pd.DataFrame) -> MarketCandidate | None:
    """Select the first row from an already filtered/sorted DataFrame."""
    if df.empty:
        return None
    row = df.iloc[0]
    return _candidate(row)


def _candidate(row: pd.Series) -> MarketCandidate:
    return MarketCandidate(
        market_id=str(row["market_id"]),
        condition_id=str(row["condition_id"]),
        question=str(row["question"]),
        yes_token_id=str(row["yes_token_id"]),
        no_token_id=str(row["no_token_id"]),
        end_date=row["end_date"],
        volume=float(row["volume"]) if pd.notna(row["volume"]) else 0.0,
        spread=float(row["spread"]) if pd.notna(row["spread"]) else 0.0,
        category=row.get("category"),
        raw=row.get("raw", {}),
        score=float(row.get("score", 0.0)),
    )


def _restore_saved_markets(
        config: Any,
        market_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Resolve legacy CSV positions, including closed markets.

    ``GET /markets`` only returns the currently listed set for this use case and
    may omit a closed market with a remaining outcome token.  The single-market
    endpoint is authoritative for a legacy holding and is used only once per
    held market during CSV migration.
    """
    result = {}
    missing = []
    with requests.Session() as session:
        for market_id in market_ids:
            response = session.get(
                f"{config.gamma_api_url.rstrip('/')}/markets/{market_id}",
                timeout=_GAMMA_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 404:
                missing.append(str(market_id))
                continue
            response.raise_for_status()
            market = response.json()
            tokens = _extract_yes_no(market) if isinstance(market, dict) else None
            if tokens is None:
                missing.append(str(market_id))
            else:
                result[str(market_id)] = market
    if missing:
        raise ValueError(
            "Cannot reconcile saved holdings; token IDs unavailable for market IDs: "
            + ", ".join(missing)
        )
    return result


def restore_token_ids(
        config: Any,
        market_ids: Iterable[str],
) -> dict[str, tuple[str, str]]:
    return {mid: _extract_yes_no(row)[:2]
            for mid, row in _restore_saved_markets(config, market_ids).items()}


def restore_inventory_markets(
        config: Any,
        market_ids: Iterable[str],
) -> dict[str, MarketCandidate]:
    """Recover lifecycle metadata for holdings even when discovery excludes them."""
    result = {}
    for mid, row in _restore_saved_markets(config, market_ids).items():
        end = _parse_datetime(row.get("endDate") or row.get("end_date_iso"))
        if not row.get("conditionId") or end is None:
            raise ValueError(f"Cannot manage saved inventory; missing lifecycle metadata for {mid}")
        yes, no = _extract_yes_no(row)[:2]
        result[mid] = MarketCandidate(mid, str(row["conditionId"]), str(row.get("question", "")),
                                      yes, no, end, 0.0, 0.0, raw=row)
    return result
