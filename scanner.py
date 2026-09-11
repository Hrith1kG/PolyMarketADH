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

# "Require High Confidence": a stricter version of the same idea. This setting was
# writable from the dashboard but read by nothing, so turning it on changed no
# behaviour at all. It now demands a tight two-sided market AND enough resting size
# at the best ask to actually fill the intended stake -- the depth check also stops
# the scanner surfacing prices no order could be filled at.
CONFIDENCE_MAX_SPREAD = 0.01
CONFIDENCE_MAX_QUOTE_AGE_SECONDS = 60
CONFIDENCE_DEPTH_MULTIPLE = 1.0

# Set by find_opportunities when a scan fails, so callers can distinguish "the market
# had nothing" from "the scan broke". Failures used to be print()ed and swallowed,
# which the dashboard rendered indistinguishably from a clean empty scan.
LAST_SCAN_ERROR: Optional[str] = None


def _orderbook_health_price(
    order_book: Any,
    require_high_confidence: bool = False,
    required_shares: float = 0.0,
) -> Optional[float]:
    """Returns the live best-ask price if this token's CLOB order book looks
    healthy (two-sided, tight spread, fresh quote); otherwise None.

    With require_high_confidence the spread and staleness limits tighten and the best
    ask must also hold at least `required_shares` of resting size.
    """
    if not order_book.bids or not order_book.asks:
        return None

    max_spread = CONFIDENCE_MAX_SPREAD if require_high_confidence else HEALTH_MAX_SPREAD
    max_age = CONFIDENCE_MAX_QUOTE_AGE_SECONDS if require_high_confidence else HEALTH_MAX_QUOTE_AGE_SECONDS

    # SDK documents bids ascending (best last) and asks descending (best last).
    best_bid = float(order_book.bids[-1].price)
    best_ask = float(order_book.asks[-1].price)
    if (best_ask - best_bid) > max_spread:
        return None

    if order_book.timestamp is not None:
        ts = order_book.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_seconds > max_age:
            return None

    if require_high_confidence and required_shares > 0:
        ask_size = float(order_book.asks[-1].size or 0.0)
        if ask_size < required_shares * CONFIDENCE_DEPTH_MULTIPLE:
            return None

    # The book's `min_order_size` is intentionally not used to filter signals out.
    # Gating on it silently hid every signal whenever stake_per_trade was small, and
    # it does not actually predict rejection: orders well below the reported
    # min_order_size fill on this exchange in practice.
    return best_ask


def _late_game_ok(
    game_start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    threshold_seconds: float,
    require_authoritative_time: bool,
) -> bool:
    """"Late game" means the event is actually underway AND close to resolving --
    not just that its resolution deadline happens to fall in some wide window.
    Guards against entering a heavy favorite priced high before kickoff, or a
    market resolving hours/days from now."""
    if end_dt is None:
        return False  # no resolution time to judge "imminent" against

    now = datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    if (end_dt - now).total_seconds() > threshold_seconds:
        return False

    if game_start_dt is None:
        # No confirmed kickoff time for this game: only proceed on resolution
        # proximity alone if we're not required to authoritatively confirm start.
        return not require_authoritative_time

    if game_start_dt.tzinfo is None:
        game_start_dt = game_start_dt.replace(tzinfo=timezone.utc)
    return game_start_dt <= now


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
    global LAST_SCAN_ERROR
    LAST_SCAN_ERROR = None
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
    require_high_confidence = bool(settings.get("require_high_confidence", False))
    # Depth/minimum-size checks need to know how big an order this signal would
    # produce, so price the intended stake into shares before probing the book.
    reference_stake = float(settings.get("stake_per_trade", 25.0) or 0.0)

    late_game_enabled = bool(settings.get("late_game_enabled", False))
    require_authoritative_time = bool(settings.get("require_authoritative_time", False))
    late_game_threshold_seconds = float(settings.get("late_game_threshold_seconds", 600))
    # The generic resolution-window floor (min_hours) measures time to the
    # market's resolution deadline, which is a different thing from "the game
    # is close to over" -- with Late Game on, the dedicated threshold below
    # replaces that floor instead of stacking with it.
    effective_min_hours = 0.0 if late_game_enabled else min_hours

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
                    if not _within_resolution_window(market.state.end_date, effective_min_hours, max_days):
                        continue
                    if late_game_enabled:
                        game_start_dt = market.sports.game_start_time if market.sports else None
                        if not _late_game_ok(game_start_dt, market.state.end_date, late_game_threshold_seconds, require_authoritative_time):
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
                    if require_healthy_data or require_high_confidence:
                        try:
                            order_book = client.get_order_book(token_id=token_id_str)
                        except (RateLimitError, PolymarketError):
                            continue
                        required_shares = (reference_stake / gamma_price) if gamma_price > 0 else 0.0
                        confirmed_price = _orderbook_health_price(
                            order_book,
                            require_high_confidence=require_high_confidence,
                            required_shares=required_shares,
                        )
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
        # Record the failure as well as printing it. A swallowed error used to be
        # indistinguishable, in the dashboard, from a scan that simply found nothing.
        LAST_SCAN_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"[scanner] Error scanning sports markets: {exc}")

    opportunities.sort(key=lambda o: o.confirmed_price, reverse=True)
    if max_signals > 0:
        return opportunities[:max_signals]
    return opportunities
