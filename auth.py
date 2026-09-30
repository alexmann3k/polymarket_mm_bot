from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from py_clob_client_v2 import ApiCreds, ClobClient
from py_clob_client_v2.exceptions import PolyApiException

from config import Config

logger = logging.getLogger(__name__)
RETRY_DELAY_SECONDS = 0.25


class ResilientClobClient(ClobClient):
    """Retry a transient GET once; never replay an order submission."""

    def _get(self, endpoint: str, headers: Any = None, params: Any = None) -> Any:
        try:
            return super()._get(endpoint, headers=headers, params=params)
        except PolyApiException as exc:
            status = exc.status_code
            transient = (status is not None and 500 <= status < 600) or isinstance(
                exc.__cause__ or exc.__context__,
                (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
            if not transient:
                raise
            logger.warning("Transient CLOB read failure; retrying once")
            time.sleep(RETRY_DELAY_SECONDS)
            return super()._get(endpoint, headers=headers, params=params)


def _api_creds(config: Config) -> ApiCreds | None:
    values = (config.clob_api_key, config.clob_secret, config.clob_passphrase)
    if not all(values):
        return None
    return ApiCreds(
        api_key=config.clob_api_key,
        api_secret=config.clob_secret,
        api_passphrase=config.clob_passphrase,
    )


def build_client(config: Config, *, require_l2: bool = True) -> ClobClient:
    if not config.pk:
        raise ValueError("PK is missing. Put the private key in .env.")

    creds = _api_creds(config)
    if require_l2 and creds is None:
        raise ValueError("CLOB API credentials are required for authenticated operations.")

    return ResilientClobClient(
        host=config.clob_api_url,
        chain_id=config.chain_id,
        key=config.pk,
        creds=creds,
        signature_type=config.signature_type,
        funder=config.funder or None,
        retry_on_error=False,
    )


def bootstrap_client(config: Config) -> ClobClient:
    """Create/derive L2 credentials and return a fully authenticated client."""
    client = build_client(config, require_l2=False)
    if client.creds is not None:
        return client

    try:
        derived = client.create_or_derive_api_key()
        client.set_api_creds(derived)
        logger.info("CLOB API key created/derived successfully.")
        return client
    except Exception as exc:
        logger.exception("Failed to create/derive CLOB API credentials: %s", exc)
        raise
