"""Read-only access to Polymarket's public CLOB REST API (no wallet/API key needed
for price/book lookups -- only order placement requires auth, see live_broker.py)."""
import requests

import config


def get_price(token_id, side="BUY", timeout=10):
    """Best current price for a token on the given side ("BUY" or "SELL").
    Returns None if the book has no liquidity on that side or the request fails."""
    try:
        resp = requests.get(
            f"{config.CLOB_BASE_URL}/price",
            params={"token_id": token_id, "side": side},
            timeout=timeout,
        )
        resp.raise_for_status()
        price = resp.json().get("price")
        return float(price) if price is not None else None
    except (requests.RequestException, TypeError, ValueError):
        return None
