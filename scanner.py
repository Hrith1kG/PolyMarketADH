"""Finds outcomes that look 'almost confirmed' by price, above the liquidity/volume
bar, and not about to resolve too soon or too far out."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import clob_client
import config
import gamma_client


@dataclass
class Opportunity:
    market_id: str
    question: str
    slug: str
    outcome_label: str
    token_id: str
    gamma_price: float
    confirmed_price: float
    volume: float
    liquidity: float
    end_date: Optional[str]


def _parse_end_date(end_date_str):
    if not end_date_str:
        return None
    try:
        return datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_resolution_window(end_date_str):
    end_dt = _parse_end_date(end_date_str)
    if end_dt is None:
        return True  # unknown end date -- don't filter it out on this basis alone
    now = datetime.now(timezone.utc)
    delta = end_dt - now
    if delta < timedelta(hours=config.MIN_HOURS_TO_RESOLUTION):
        return False
    if delta > timedelta(days=config.MAX_DAYS_TO_RESOLUTION):
        return False
    return True


def find_opportunities(held_token_ids=None):
    """Scans all active markets and returns a list of Opportunity, freshest-checked
    against the live order book, sorted by highest confirmed price first."""
    held_token_ids = held_token_ids or set()
    opportunities = []

    for outcome in gamma_client.fetch_all_outcomes():
        if outcome["token_id"] in held_token_ids:
            continue
        if not (config.PRICE_MIN <= outcome["price"] <= config.PRICE_MAX):
            continue
        if outcome["volume"] < config.MIN_VOLUME:
            continue
        if outcome["liquidity"] < config.MIN_LIQUIDITY:
            continue
        if not _within_resolution_window(outcome["end_date"]):
            continue

        # Gamma's price can lag; confirm against the live CLOB book before trusting it.
        confirmed = clob_client.get_price(outcome["token_id"], side="BUY")
        if confirmed is None:
            continue
        if not (config.PRICE_MIN <= confirmed <= config.PRICE_MAX):
            continue

        opportunities.append(Opportunity(
            market_id=outcome["market_id"],
            question=outcome["question"],
            slug=outcome["slug"],
            outcome_label=outcome["outcome_label"],
            token_id=outcome["token_id"],
            gamma_price=outcome["price"],
            confirmed_price=confirmed,
            volume=outcome["volume"],
            liquidity=outcome["liquidity"],
            end_date=outcome["end_date"],
        ))

    opportunities.sort(key=lambda o: o.confirmed_price, reverse=True)
    return opportunities
