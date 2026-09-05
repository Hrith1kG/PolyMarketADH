"""Read-only access to Polymarket's public Gamma API (no wallet/API key needed)."""
import json
import time

import requests

import config


class GammaError(Exception):
    pass


def _parse_json_field(raw, field, default):
    val = raw.get(field, default)
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return default
    return val if val is not None else default


def fetch_markets_page(limit=200, offset=0, retries=3):
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "offset": offset,
    }
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(f"{config.GAMMA_BASE_URL}/markets", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise GammaError(f"Failed to fetch markets: {last_exc}")


def fetch_all_active_markets(max_pages=25, page_size=200):
    """Paginates through Gamma's /markets until an empty page or max_pages is hit."""
    markets = []
    offset = 0
    for _ in range(max_pages):
        page = fetch_markets_page(limit=page_size, offset=offset)
        if not page:
            break
        markets.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return markets


def parse_market(raw):
    """Normalizes a raw Gamma market dict into outcome-level records."""
    outcomes = _parse_json_field(raw, "outcomes", [])
    prices = _parse_json_field(raw, "outcomePrices", [])
    token_ids = _parse_json_field(raw, "clobTokenIds", [])

    if not (len(outcomes) == len(prices) == len(token_ids)) or not outcomes:
        return []

    records = []
    for label, price_str, token_id in zip(outcomes, prices, token_ids):
        try:
            price = float(price_str)
        except (TypeError, ValueError):
            continue
        records.append({
            "market_id": raw.get("id"),
            "question": raw.get("question", "").strip(),
            "slug": raw.get("slug", ""),
            "outcome_label": label,
            "price": price,
            "token_id": str(token_id),
            "volume": float(raw.get("volume") or 0),
            "liquidity": float(raw.get("liquidity") or 0),
            "end_date": raw.get("endDate"),
        })
    return records


def fetch_all_outcomes():
    """Returns a flat list of outcome-level dicts across every active market."""
    outcomes = []
    for raw in fetch_all_active_markets():
        outcomes.extend(parse_market(raw))
    return outcomes
