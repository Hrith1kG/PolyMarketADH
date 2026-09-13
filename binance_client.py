"""Binance Spot API integration for the PolyMarketADH crypto strategy.

Fetches real-time spot prices from Binance public REST API (/api/v3/ticker/price)
to enable pre-trade Oracle front-running checks.
"""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

BINANCE_API_BASE = "https://api.binance.com"
BINANCE_US_API_BASE = "https://api.binance.us"
DEFAULT_TIMEOUT = 1.5

# Standard asset symbol mappings for approved assets and common crypto markets
ASSET_TO_BINANCE_SYMBOL: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "BITCOIN": "BTCUSDT",
    "XBT": "BTCUSDT",
    "ETH": "ETHUSDT",
    "ETHEREUM": "ETHUSDT",
    "ETHER": "ETHUSDT",
    "SOL": "SOLUSDT",
    "SOLANA": "SOLUSDT",
    "XRP": "XRPUSDT",
    "RIPPLE": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "DOGECOIN": "DOGEUSDT",
    "AVAX": "AVAXUSDT",
    "AVALANCHE": "AVAXUSDT",
    "BNB": "BNBUSDT",
    "BINANCECOIN": "BNBUSDT",
    "LINK": "LINKUSDT",
    "CHAINLINK": "LINKUSDT",
    "ADA": "ADAUSDT",
    "CARDANO": "ADAUSDT",
    "MATIC": "MATICUSDT",
    "POLYGON": "MATICUSDT",
    "POL": "POLUSDT",
    "DOT": "DOTUSDT",
    "POLKADOT": "DOTUSDT",
    "LTC": "LTCUSDT",
    "LITECOIN": "LTCUSDT",
    "BCH": "BCHUSDT",
    "BITCOINCASH": "BCHUSDT",
    "SHIB": "SHIBUSDT",
    "SHIBA": "SHIBUSDT",
    "SHIBAINU": "SHIBUSDT",
    "TRX": "TRXUSDT",
    "TRON": "TRXUSDT",
    "NEAR": "NEARUSDT",
    "NEARPROTOCOL": "NEARUSDT",
    "ATOM": "ATOMUSDT",
    "COSMOS": "ATOMUSDT",
    "UNI": "UNIUSDT",
    "UNISWAP": "UNIUSDT",
    "APT": "APTUSDT",
    "APTOS": "APTUSDT",
    "SUI": "SUIUSDT",
    "PEPE": "PEPEUSDT",
    "RENDER": "RENDERUSDT",
    "RNDR": "RENDERUSDT",
    "INJ": "INJUSDT",
    "INJECTIVE": "INJUSDT",
    "TIA": "TIAUSDT",
    "CELESTIA": "TIAUSDT",
    "ARB": "ARBUSDT",
    "ARBITRUM": "ARBUSDT",
    "OP": "OPUSDT",
    "OPTIMISM": "OPUSDT",
    "TON": "TONUSDT",
    "TONCOIN": "TONUSDT",
}


def to_binance_symbol(asset_or_symbol: str) -> str:
    """Normalizes an asset name or symbol into a Binance USDT ticker symbol.

    Examples:
        'BTC' -> 'BTCUSDT'
        'btc' -> 'BTCUSDT'
        'ETH' -> 'ETHUSDT'
        'BTCUSDT' -> 'BTCUSDT'
        'ETH/USDT' -> 'ETHUSDT'
        'BTC/USD' -> 'BTCUSDT'
        'BTC/USDC' -> 'BTCUSDT'
        'Avalanche' -> 'AVAXUSDT'
        'Binance Coin' -> 'BNBUSDT'
    """
    clean = re.sub(r"[\s/_-]+", "", str(asset_or_symbol or "").strip().upper())
    if clean.endswith("USDC"):
        clean = clean[:-4]
    elif clean.endswith("USD") and not clean.endswith("USDT"):
        clean = clean[:-3]
    elif clean.endswith("PERP"):
        clean = clean[:-4]

    if clean in ASSET_TO_BINANCE_SYMBOL:
        return ASSET_TO_BINANCE_SYMBOL[clean]
    if clean in ("USDT", "USD", "USDC") or not clean:
        return ""
    if clean.endswith("USDT"):
        return clean
    return f"{clean}USDT"


class BinanceClient:
    """Lightweight REST client for Binance public market data."""

    def __init__(
        self,
        base_url: str = BINANCE_API_BASE,
        fallback_base_url: Optional[str] = BINANCE_US_API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.fallback_base_url = fallback_base_url.rstrip("/") if fallback_base_url else None
        self.timeout = timeout

    def _fetch_from_base(
        self,
        base_url: str,
        symbol: str,
        timeout: Optional[float] = None,
    ) -> Optional[float]:
        url = f"{base_url}/api/v3/ticker/price?symbol={symbol}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PolyMarketADH-Bot/1.0",
                "Accept": "application/json",
            },
        )
        t = timeout if timeout is not None and timeout > 0 else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                if resp.status != 200:
                    return None
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                if isinstance(data, dict) and "price" in data:
                    price = float(data["price"])
                    if math.isfinite(price) and price > 0:
                        return price
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError):
            return None
        except Exception:
            return None

        return None

    def get_spot_price(
        self,
        asset_or_symbol: str,
        timeout: Optional[float] = None,
        allow_fallback: bool = True,
    ) -> Optional[float]:
        """Fetches the latest spot price for a given asset from Binance.

        Returns float price on success, or None on failure / network error.
        Attempts primary base_url first, falling back to fallback_base_url if configured and allowed.
        """
        symbol = to_binance_symbol(asset_or_symbol)
        if not symbol or symbol == "USDT":
            return None

        price = self._fetch_from_base(self.base_url, symbol, timeout=timeout)
        if price is not None:
            return price

        if allow_fallback and self.fallback_base_url and self.fallback_base_url != self.base_url:
            price = self._fetch_from_base(self.fallback_base_url, symbol, timeout=timeout)
            if price is not None:
                return price

        return None


# Module-level default client
_DEFAULT_CLIENT = BinanceClient()


def get_binance_spot_price(
    asset_or_symbol: str,
    timeout: Optional[float] = None,
    base_url: str = BINANCE_API_BASE,
    allow_fallback: bool = True,
) -> Optional[float]:
    """Convenience function to fetch Binance spot price."""
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    if effective_timeout != DEFAULT_TIMEOUT or base_url != BINANCE_API_BASE or not allow_fallback:
        return BinanceClient(
            base_url=base_url,
            fallback_base_url=BINANCE_US_API_BASE if allow_fallback else None,
            timeout=effective_timeout,
        ).get_spot_price(asset_or_symbol, timeout=effective_timeout, allow_fallback=allow_fallback)
    return _DEFAULT_CLIENT.get_spot_price(asset_or_symbol)
