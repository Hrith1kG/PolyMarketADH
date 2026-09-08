"""Finds outcomes that look 'almost confirmed' by price, above the liquidity/volume
floor, and within the desired resolution window, focusing on sports moneylines."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Set, Dict, Any

from polymarket import RateLimitError, PolymarketError
import polymarket_client
import settings_manager


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
    market_type: str = "generic"
    game_start_time: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _within_resolution_window(end_dt: Optional[datetime], min_hours: float, max_days: float) -> bool:
    if end_dt is None:
        return True
    now = datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    delta = end_dt - now
    if delta < timedelta(hours=min_hours):
        return False
    if delta > timedelta(days=max_days):
        return False
    return True


# "Require Healthy Data" thresholds: a single stale trade can leave a thin market
# showing e.g. 0.98 with no one actually willing to trade there. A two-sided book
# with a tight spread and a fresh quote is what tells us that price is real.
HEALTH_MAX_SPREAD = 0.03
HEALTH_MAX_QUOTE_AGE_SECONDS = 120


def _orderbook_health_price(order_book: Any) -> Optional[float]:
    """Returns the live best-ask price if this token's CLOB order book looks
    healthy (two-sided, tight spread, fresh quote); otherwise None."""
    if not order_book.bids or not order_book.asks:
        return None

    best_bid = float(order_book.bids[-1].price)
    best_ask = float(order_book.asks[-1].price)
    if (best_ask - best_bid) > HEALTH_MAX_SPREAD:
        return None

    if order_book.timestamp is not None:
        ts = order_book.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_seconds > HEALTH_MAX_QUOTE_AGE_SECONDS:
            return None

    return best_ask


def find_opportunities(
    held_token_ids: Optional[Set[str]] = None,
    max_pages: int = 25,
    page_size: int = 100,
    settings_override: Optional[Dict[str, Any]] = None,
) -> List[Opportunity]:
    """Scans markets via the unified SDK applying runtime dashboard filters,
    re-confirmed against the live CLOB order book and sorted by highest confirmed price.
    Pass settings_override (e.g. a union of several accounts' thresholds) to scan a
    broader net than the global settings in one pass."""
    held_token_ids = held_token_ids or set()
    opportunities = []
    client = polymarket_client.get_public_client()
    settings = settings_override or settings_manager.load_settings()

    price_min = settings.get("price_min", 0.97)
    price_max = settings.get("price_max", 0.995)
    min_volume = settings.get("min_volume", 5000.0)
    min_liquidity = settings.get("min_liquidity", 1000.0)
    min_hours = settings.get("min_hours_to_resolution", 1.0)
    max_days = settings.get("max_days_to_resolution", 30.0)
    max_signals = settings.get("max_signals_per_scan", 5)

    only_sports = settings.get("only_sports", True)
    sports_types = settings.get("sports_market_types", ["moneyline"])
    sports_tag = settings.get("sports_tag_id", 100639)
    require_healthy_data = bool(settings.get("require_healthy_data", True))

    try:
        # Pass sports filters directly to the API when enabled
        query_params: Dict[str, Any] = {
            "closed": False,
            "page_size": page_size,
        }
        if only_sports:
            query_params["tag_id"] = sports_tag
            if sports_types:
                query_params["sports_market_types"] = sports_types

        pages = client.list_markets(**query_params)
        page_count = 0

        for page in pages:
            page_count += 1
            if not page.items:
                break

            for market in page.items:
                # State checks
                if market.state:
                    if market.state.closed:
                        continue
                    if market.state.accepting_orders is False:
                        continue
                    if not _within_resolution_window(market.state.end_date, min_hours, max_days):
                        continue

                # Metrics checks
                volume = float(market.metrics.volume or 0.0) if market.metrics else 0.0
                liquidity = float(market.metrics.liquidity or 0.0) if market.metrics else 0.0

                if volume < min_volume or liquidity < min_liquidity:
                    continue

                if not market.outcomes:
                    continue

                outcomes_to_check = [market.outcomes.yes, market.outcomes.no]
                for outcome in outcomes_to_check:
                    if not outcome or not outcome.token_id:
                        continue

                    token_id_str = str(outcome.token_id)
                    if token_id_str in held_token_ids:
                        continue

                    if outcome.price is None:
                        continue
                    gamma_price = float(outcome.price)
                    if not (price_min <= gamma_price <= price_max):
                        continue

                    # Re-confirm against the live CLOB book. With "Require Healthy
                    # Data" on, pull the actual order book so we can also verify
                    # it's a live, tight, two-sided quote -- not just one stale print.
                    if require_healthy_data:
                        try:
                            order_book = client.get_order_book(token_id=token_id_str)
                        except (RateLimitError, PolymarketError):
                            continue
                        confirmed_price = _orderbook_health_price(order_book)
                        if confirmed_price is None:
                            continue
                    else:
                        try:
                            confirmed_dec = client.get_price(token_id=token_id_str, side="BUY")
                        except (RateLimitError, PolymarketError):
                            continue
                        if confirmed_dec is None:
                            continue
                        confirmed_price = float(confirmed_dec)

                    if not (price_min <= confirmed_price <= price_max):
                        continue

                    end_date_str = market.state.end_date.isoformat() if (market.state and market.state.end_date) else None
                    start_time_str = market.sports.game_start_time.isoformat() if (market.sports and market.sports.game_start_time) else None
                    market_type_str = market.sports.sports_market_type if (market.sports and market.sports.sports_market_type) else "moneyline"
                    question = market.question or market.slug or "Unknown Match"

                    opportunities.append(Opportunity(
                        market_id=str(market.id),
                        question=question,
                        slug=market.slug or "",
                        outcome_label=outcome.label,
                        token_id=token_id_str,
                        gamma_price=gamma_price,
                        confirmed_price=confirmed_price,
                        volume=volume,
                        liquidity=liquidity,
                        end_date=end_date_str,
                        market_type=market_type_str,
                        game_start_time=start_time_str,
                    ))

            if page_count >= max_pages:
                break

    except Exception as exc:
        print(f"[scanner] Error scanning sports markets: {exc}")

    opportunities.sort(key=lambda o: o.confirmed_price, reverse=True)
    if max_signals > 0:
        return opportunities[:max_signals]
    return opportunities
