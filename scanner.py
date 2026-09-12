"""Finds outcomes that look 'almost confirmed' by price, above the liquidity/volume
floor, and -- with Late Game enabled -- inside a live match that is genuinely close to
finishing, focusing on sports moneylines."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Set, Dict, Any

from polymarket import RateLimitError, PolymarketError
import polymarket_client
import settings_manager
import live_timing


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
    # Informational only. end_date is never used to judge how far a match has
    # progressed -- it is start_time for soccer, start+7d for tennis and baseball,
    # and start+6h for esports, so it carries no signal about the end of play.
    end_date: Optional[str]
    market_type: str = "generic"
    game_start_time: Optional[str] = None
    # Populated on the Late Game path so the dashboard can show why this qualified.
    sport: Optional[str] = None
    remaining_minutes: Optional[float] = None
    timing_basis: Optional[str] = None
    timing_detail: Optional[str] = None

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
#
# These quote-age limits establish that MARKET DATA is current. They say nothing
# about how far the match has progressed -- game timing is decided entirely by
# live_timing from in-play state, and never reads an order-book timestamp.
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

# Every candidate rejected during the last scan, with a machine-readable reason. An
# empty scan is otherwise unexplainable from the outside: this says whether nothing
# was live, everything was too early, or the prices were simply out of band.
LAST_SCAN_REJECTIONS: List[Dict[str, Any]] = []

REJECT_NOT_LIVE = "not_live"
REJECT_MARKET_FILTERED = "market_filtered"
REJECT_LIQUIDITY_OR_VOLUME = "liquidity_or_volume"
REJECT_ALREADY_HELD = "already_held"
REJECT_PROBABILITY = "probability_out_of_band"
REJECT_UNHEALTHY_BOOK = "unhealthy_book"


def _reject(reason: str, subject: str, detail: str, quiet: bool = False) -> None:
    """Records why one candidate did not make it through, and normally prints it.

    `quiet` records without printing, for the high-volume routine filters (wrong
    market type, outside the resolution window) that would otherwise bury the
    interesting rejections under thousands of lines on a full scan. They are still
    counted in LAST_SCAN_REJECTIONS and in rejection_summary().
    """
    LAST_SCAN_REJECTIONS.append({"reason": reason, "subject": subject, "detail": detail})
    if not quiet:
        print(f"[scanner] reject [{reason}] {subject}: {detail}")


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


def _confirm_price(
    client: Any,
    token_id: str,
    gamma_price: float,
    subject: str,
    require_healthy_data: bool,
    require_high_confidence: bool,
    reference_stake: float,
) -> Optional[float]:
    """Re-confirms a Gamma price against the live CLOB, applying the health gates.

    Returns None -- having logged why -- when the book is unusable or unhealthy.
    """
    if require_healthy_data or require_high_confidence:
        try:
            order_book = client.get_order_book(token_id=token_id)
        except (RateLimitError, PolymarketError) as exc:
            _reject(REJECT_UNHEALTHY_BOOK, subject, f"order book unavailable: {type(exc).__name__}")
            return None
        required_shares = (reference_stake / gamma_price) if gamma_price > 0 else 0.0
        confirmed = _orderbook_health_price(
            order_book,
            require_high_confidence=require_high_confidence,
            required_shares=required_shares,
        )
        if confirmed is None:
            _reject(REJECT_UNHEALTHY_BOOK, subject,
                    "book failed spread / two-sidedness / quote-freshness"
                    + (" / depth" if require_high_confidence else "") + " checks")
        return confirmed

    try:
        confirmed_dec = client.get_price(token_id=token_id, side="BUY")
    except (RateLimitError, PolymarketError) as exc:
        _reject(REJECT_UNHEALTHY_BOOK, subject, f"price unavailable: {type(exc).__name__}")
        return None
    if confirmed_dec is None:
        _reject(REJECT_UNHEALTHY_BOOK, subject, "no BUY price returned")
        return None
    return float(confirmed_dec)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _scan_live_events(
    client: Any,
    settings: Dict[str, Any],
    held_token_ids: Set[str],
    max_pages: int,
    page_size: int,
) -> List[Opportunity]:
    """Late Game scan: only matches the server reports as live, timed per sport.

    Live in-play state (period / elapsed / score) exists on the Event and is absent
    from the Market model entirely, so this path discovers candidates through the
    server-side live-event filter rather than list_markets. end_date plays no part in
    any decision made here.
    """
    opportunities: List[Opportunity] = []

    min_volume = float(settings.get("min_volume", 5000.0))
    min_liquidity = float(settings.get("min_liquidity", 1000.0))
    only_sports = bool(settings.get("only_sports", True))
    sports_types = settings.get("sports_market_types", ["moneyline"]) or []
    sports_tag = settings.get("sports_tag_id", 100639)
    require_healthy_data = bool(settings.get("require_healthy_data", True))
    require_high_confidence = bool(settings.get("require_high_confidence", False))
    reference_stake = float(settings.get("stake_per_trade", 25.0) or 0.0)

    # One band, resolved in one place. With Late Game on it comes from the Late Game
    # probability settings and price_min/price_max do not apply at all, so a
    # configured 0.90-0.92 band means exactly 0.90-0.92.
    prob_min, prob_max = settings_manager.effective_price_band(settings)

    query: Dict[str, Any] = {"live": True, "closed": False, "page_size": page_size}
    if only_sports and sports_tag:
        query["tag_ids"] = sports_tag

    pages = client.list_events(**query)
    page_count = 0

    for page in pages:
        page_count += 1
        if not page.items:
            break

        for event in page.items:
            title = event.title or event.slug or f"event {event.id}"

            decision = live_timing.evaluate_event_timing(event, settings)
            if not decision.eligible:
                _reject(decision.reason, title, decision.detail)
                continue

            estimate = decision.estimate

            for market in (event.markets or []):
                subject = f"{title} / {market.question or market.slug or market.id}"

                market_type = (market.sports.sports_market_type
                               if market.sports else None) or "moneyline"
                if sports_types and market_type not in sports_types:
                    _reject(REJECT_MARKET_FILTERED, subject,
                            f"market type {market_type!r} not in {list(sports_types)}",
                            quiet=True)
                    continue

                if market.state:
                    if market.state.closed:
                        _reject(REJECT_MARKET_FILTERED, subject, "market is closed")
                        continue
                    if market.state.accepting_orders is False:
                        _reject(REJECT_MARKET_FILTERED, subject, "market is not accepting orders")
                        continue

                volume = float(market.metrics.volume or 0.0) if market.metrics else 0.0
                liquidity = float(market.metrics.liquidity or 0.0) if market.metrics else 0.0
                if volume < min_volume or liquidity < min_liquidity:
                    _reject(REJECT_LIQUIDITY_OR_VOLUME, subject,
                            f"volume {volume:,.0f} < {min_volume:,.0f}" if volume < min_volume
                            else f"liquidity {liquidity:,.0f} < {min_liquidity:,.0f}")
                    continue

                if not market.outcomes:
                    _reject(REJECT_MARKET_FILTERED, subject, "market has no outcomes")
                    continue

                for outcome in (market.outcomes.yes, market.outcomes.no):
                    if not outcome or not outcome.token_id:
                        continue
                    token_id = str(outcome.token_id)
                    label = f"{subject} [{outcome.label}]"

                    if token_id in held_token_ids:
                        _reject(REJECT_ALREADY_HELD, label, "a position in this token is already open")
                        continue

                    if outcome.price is None:
                        _reject(REJECT_PROBABILITY, label, "no price published")
                        continue

                    gamma_price = float(outcome.price)
                    if not (prob_min <= gamma_price <= prob_max):
                        _reject(REJECT_PROBABILITY, label,
                                f"price {gamma_price:.3f} outside band "
                                f"{prob_min:.3f}-{prob_max:.3f}")
                        continue

                    confirmed_price = _confirm_price(
                        client, token_id, gamma_price, label,
                        require_healthy_data, require_high_confidence, reference_stake,
                    )
                    if confirmed_price is None:
                        continue

                    if not (prob_min <= confirmed_price <= prob_max):
                        _reject(REJECT_PROBABILITY, label,
                                f"confirmed price {confirmed_price:.3f} outside band "
                                f"{prob_min:.3f}-{prob_max:.3f}")
                        continue

                    print(f"[scanner] accept {label}: {decision.describe()}, "
                          f"price {confirmed_price:.3f}")
                    opportunities.append(Opportunity(
                        market_id=str(market.id),
                        question=market.question or market.slug or title,
                        slug=market.slug or "",
                        outcome_label=outcome.label,
                        token_id=token_id,
                        gamma_price=gamma_price,
                        confirmed_price=confirmed_price,
                        volume=volume,
                        liquidity=liquidity,
                        end_date=_iso(market.state.end_date if market.state else None),
                        market_type=market_type,
                        game_start_time=_iso(event.schedule.start_time if event.schedule else None),
                        sport=estimate.sport,
                        remaining_minutes=round(estimate.remaining_minutes, 1),
                        timing_basis=estimate.basis,
                        timing_detail=estimate.detail,
                    ))

        if page_count >= max_pages:
            break

    return opportunities


def _scan_markets(
    client: Any,
    settings: Dict[str, Any],
    held_token_ids: Set[str],
    max_pages: int,
    page_size: int,
) -> List[Opportunity]:
    """Standard scan: every open market in the resolution window, Late Game off."""
    opportunities: List[Opportunity] = []

    price_min, price_max = settings_manager.effective_price_band(settings)
    min_volume = float(settings.get("min_volume", 5000.0))
    min_liquidity = float(settings.get("min_liquidity", 1000.0))
    min_hours = float(settings.get("min_hours_to_resolution", 1.0))
    max_days = float(settings.get("max_days_to_resolution", 30.0))
    only_sports = bool(settings.get("only_sports", True))
    sports_types = settings.get("sports_market_types", ["moneyline"])
    sports_tag = settings.get("sports_tag_id", 100639)
    require_healthy_data = bool(settings.get("require_healthy_data", True))
    require_high_confidence = bool(settings.get("require_high_confidence", False))
    # Depth/minimum-size checks need to know how big an order this signal would
    # produce, so price the intended stake into shares before probing the book.
    reference_stake = float(settings.get("stake_per_trade", 25.0) or 0.0)

    query_params: Dict[str, Any] = {"closed": False, "page_size": page_size}
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
            subject = market.question or market.slug or str(market.id)

            if market.state:
                if market.state.closed:
                    continue
                if market.state.accepting_orders is False:
                    continue
                if not _within_resolution_window(market.state.end_date, min_hours, max_days):
                    _reject(REJECT_MARKET_FILTERED, subject,
                            f"end_date outside the {min_hours:g}h-{max_days:g}d resolution window",
                            quiet=True)
                    continue

            volume = float(market.metrics.volume or 0.0) if market.metrics else 0.0
            liquidity = float(market.metrics.liquidity or 0.0) if market.metrics else 0.0
            if volume < min_volume or liquidity < min_liquidity:
                continue

            if not market.outcomes:
                continue

            for outcome in (market.outcomes.yes, market.outcomes.no):
                if not outcome or not outcome.token_id:
                    continue
                token_id = str(outcome.token_id)
                if token_id in held_token_ids:
                    continue
                if outcome.price is None:
                    continue
                gamma_price = float(outcome.price)
                if not (price_min <= gamma_price <= price_max):
                    continue

                label = f"{subject} [{outcome.label}]"
                confirmed_price = _confirm_price(
                    client, token_id, gamma_price, label,
                    require_healthy_data, require_high_confidence, reference_stake,
                )
                if confirmed_price is None:
                    continue
                if not (price_min <= confirmed_price <= price_max):
                    continue

                opportunities.append(Opportunity(
                    market_id=str(market.id),
                    question=market.question or market.slug or "Unknown Match",
                    slug=market.slug or "",
                    outcome_label=outcome.label,
                    token_id=token_id,
                    gamma_price=gamma_price,
                    confirmed_price=confirmed_price,
                    volume=volume,
                    liquidity=liquidity,
                    end_date=_iso(market.state.end_date if market.state else None),
                    market_type=(market.sports.sports_market_type
                                 if (market.sports and market.sports.sports_market_type)
                                 else "moneyline"),
                    game_start_time=_iso(market.sports.game_start_time if market.sports else None),
                ))

        if page_count >= max_pages:
            break

    return opportunities


def find_opportunities(
    held_token_ids: Optional[Set[str]] = None,
    max_pages: int = 25,
    page_size: int = 100,
    settings_override: Optional[Dict[str, Any]] = None,
) -> List[Opportunity]:
    """Scans for entry candidates applying runtime dashboard filters, re-confirmed
    against the live CLOB order book and sorted by highest confirmed price.

    With Late Game enabled the scan runs over authoritatively live events and each
    one must show, by that sport's own timing rules, that little enough real time is
    left in the match. Pass settings_override (e.g. a union of several accounts'
    thresholds) to scan a broader net than the global settings in one pass.
    """
    global LAST_SCAN_ERROR
    LAST_SCAN_ERROR = None
    LAST_SCAN_REJECTIONS.clear()
    held_token_ids = held_token_ids or set()
    opportunities: List[Opportunity] = []
    client = polymarket_client.get_public_client()
    settings = settings_override or settings_manager.load_settings()
    max_signals = settings.get("max_signals_per_scan", 5)

    try:
        if bool(settings.get("late_game_enabled", False)):
            opportunities = _scan_live_events(client, settings, held_token_ids,
                                              max_pages, page_size)
        else:
            opportunities = _scan_markets(client, settings, held_token_ids,
                                          max_pages, page_size)
    except Exception as exc:
        # Record the failure as well as printing it. A swallowed error used to be
        # indistinguishable, in the dashboard, from a scan that simply found nothing.
        LAST_SCAN_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"[scanner] Error scanning sports markets: {exc}")

    opportunities.sort(key=lambda o: o.confirmed_price, reverse=True)
    if max_signals > 0:
        return opportunities[:max_signals]
    return opportunities


def rejection_summary() -> Dict[str, int]:
    """Counts of each rejection reason from the last scan, for dashboard display."""
    summary: Dict[str, int] = {}
    for row in LAST_SCAN_REJECTIONS:
        summary[row["reason"]] = summary.get(row["reason"], 0) + 1
    return summary
