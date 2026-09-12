"""Candidate discovery for the Crypto 5-Minute strategy.

Separate from scanner.py by design: this strategy has its own universe (five
approved coins), its own market shape (fixed 5-minute Up/Down rounds), its own
timing rule (a hard entry window measured against the authoritative round-end
timestamp) and its own price rule (an executable ask at or above a probability
floor). Nothing here reads or writes the sports settings, and scanner.py is not
imported, so the two strategies cannot drift into each other.

The scan is deliberately cheap. Rounds are listed by resolution time, which
bounds the result set to the handful of markets closing in the next few
minutes, and an order book is only pulled for a side that could plausibly clear
the probability floor. That keeps a 2-5 second polling cadence inside sensible
request budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from polymarket import RateLimitError, PolymarketError

import crypto_markets
import polymarket_client
import settings_manager
from crypto_markets import (
    CryptoRound,
    MarketRejected,
    REASON_ALREADY_TRADED,
    REASON_BELOW_MIN_LIQUIDITY,
    REASON_BELOW_MIN_VOLUME,
    REASON_BOOK_ONE_SIDED,
    REASON_BOOK_STALE,
    REASON_BOOK_THIN,
    REASON_BOOK_WIDE_SPREAD,
    REASON_DUPLICATE_POSITION,
    REASON_NO_BOOK,
    REASON_NOT_LIVE,
    REASON_PRICE_ABOVE_CEILING,
    REASON_PRICE_BELOW_THRESHOLD,
    SIDE_DOWN,
    SIDE_UP,
)

# Gamma's cached outcome price is used only to decide whether pulling a live
# order book for that side is worth a request. It is never the price a trade is
# judged on -- the executable best ask is. The margin is generous so a stale
# cache cannot hide a side that has since run up to the floor.
GAMMA_PREFILTER_MARGIN = 0.10

# The market type stamped on crypto positions, so the crypto book is
# distinguishable from sports moneylines everywhere downstream (state.json,
# trades.db, the dashboard) without inspecting slugs.
CRYPTO_MARKET_TYPE = "crypto_5m_up_down"

# Set when a scan fails, mirroring scanner.LAST_SCAN_ERROR, so the caller can
# tell "no qualifying round" apart from "the scan broke".
LAST_SCAN_ERROR: Optional[str] = None


@dataclass
class CryptoOpportunity:
    """One tradable side of one live 5-minute round.

    Field names mirror scanner.Opportunity where they overlap so the existing
    broker, database and dashboard paths accept it unchanged.
    """

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
    market_type: str = CRYPTO_MARKET_TYPE
    game_start_time: Optional[str] = None
    # Crypto-specific context
    asset: str = ""
    side: str = ""
    round_key: str = ""
    seconds_remaining: float = 0.0
    best_bid: Optional[float] = None
    ask_size: float = 0.0
    duration_source: str = ""
    strategy: str = "crypto_5m"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SkippedMarket:
    """A market the scan looked at and refused, with the reason why."""

    reason: str
    detail: str
    asset: str = ""
    slug: str = ""
    market_id: str = ""
    side: str = ""

    def describe(self) -> str:
        label = self.slug or self.market_id or "?"
        who = f"{self.asset} " if self.asset else ""
        side = f"[{self.side}] " if self.side else ""
        return f"{who}{side}{label}: {self.reason} ({self.detail})" if self.detail else f"{who}{side}{label}: {self.reason}"

    @property
    def throttle_key(self) -> str:
        """Identity of this skip, free of the varying numbers in `detail`, so a
        caller can suppress the same message repeating on every fast poll."""
        return f"{self.market_id or self.slug}:{self.side}:{self.reason}"


@dataclass
class CryptoScanResult:
    opportunities: List[CryptoOpportunity] = field(default_factory=list)
    skipped: List[SkippedMarket] = field(default_factory=list)
    rounds_seen: int = 0
    error: Optional[str] = None


def crypto_settings(settings_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reads just the crypto strategy's own settings.

    Every key is namespaced `crypto_*`; no sports key is consulted, so changing
    a sports threshold can never move a crypto gate and vice versa.
    """
    settings = settings_override if settings_override is not None else settings_manager.load_settings()
    defaults = settings_manager.DEFAULT_SETTINGS

    def _get(key: str) -> Any:
        value = settings.get(key)
        return defaults.get(key) if value is None else value

    return {
        "enabled": bool(_get("crypto_enabled")),
        "status": str(_get("crypto_bot_status") or "RUNNING").upper(),
        "kill_switch": bool(_get("crypto_entry_kill_switch")),
        "assets": list(_get("crypto_assets") or []),
        "entry_window_seconds": float(_get("crypto_entry_window_seconds")),
        "min_probability": float(_get("crypto_min_probability")),
        "max_probability": float(_get("crypto_max_probability")),
        "poll_interval_seconds": float(_get("crypto_poll_interval_seconds")),
        "stake_per_trade": float(_get("crypto_stake_per_trade")),
        "max_open_positions": int(_get("crypto_max_open_positions")),
        "max_total_exposure": float(_get("crypto_max_total_exposure")),
        "max_trades_per_day": int(_get("crypto_max_trades_per_day")),
        "max_slippage": float(_get("crypto_max_slippage")),
        "min_volume": float(_get("crypto_min_volume")),
        "min_liquidity": float(_get("crypto_min_liquidity")),
        "max_spread": float(_get("crypto_max_spread")),
        "max_quote_age_seconds": float(_get("crypto_max_quote_age_seconds")),
        "min_ask_depth_multiple": float(_get("crypto_min_ask_depth_multiple")),
        "round_duration_seconds": float(_get("crypto_round_duration_seconds")),
        "round_duration_tolerance_seconds": float(_get("crypto_round_duration_tolerance_seconds")),
        "discovery_lookahead_seconds": float(_get("crypto_discovery_lookahead_seconds")),
        "discovery_tag_id": _get("crypto_discovery_tag_id"),
        "discovery_page_size": int(_get("crypto_discovery_page_size")),
        "discovery_max_pages": int(_get("crypto_discovery_max_pages")),
        "order_type": str(_get("crypto_order_type") or "LIMIT").upper(),
    }


def evaluate_book(
    order_book: Any,
    required_shares: float,
    max_spread: float,
    max_quote_age_seconds: float,
    min_depth_multiple: float,
    now: Optional[datetime] = None,
) -> Tuple[Optional[float], str, str, Dict[str, Any]]:
    """Judges a live CLOB book and returns (executable_ask, reason, detail, info).

    `executable_ask` is the best ask only when the book is two-sided, tight,
    fresh and deep enough to absorb `required_shares`. A price no order could
    actually be filled at is not a price this strategy will act on.
    """
    info: Dict[str, Any] = {"best_bid": None, "best_ask": None, "ask_size": 0.0, "spread": None, "age": None}

    bids = getattr(order_book, "bids", None) or []
    asks = getattr(order_book, "asks", None) or []
    if not bids or not asks:
        return None, REASON_BOOK_ONE_SIDED, "book is missing a bid or an ask side", info

    # SDK documents bids ascending (best last) and asks descending (best last).
    best_bid = float(bids[-1].price)
    best_ask = float(asks[-1].price)
    ask_size = float(getattr(asks[-1], "size", 0.0) or 0.0)
    spread = best_ask - best_bid
    info.update({"best_bid": best_bid, "best_ask": best_ask, "ask_size": ask_size, "spread": spread})

    if max_spread > 0 and spread > max_spread:
        return None, REASON_BOOK_WIDE_SPREAD, f"spread {spread:.4f} > {max_spread:.4f}", info

    timestamp = getattr(order_book, "timestamp", None)
    if timestamp is not None and max_quote_age_seconds > 0:
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        age = ((now or crypto_markets.utc_now()) - ts).total_seconds()
        info["age"] = age
        if age > max_quote_age_seconds:
            return None, REASON_BOOK_STALE, f"quote is {age:.1f}s old > {max_quote_age_seconds:.0f}s", info

    if required_shares > 0 and min_depth_multiple > 0:
        needed = required_shares * min_depth_multiple
        if ask_size < needed:
            return None, REASON_BOOK_THIN, f"{ask_size:.2f} shares at the ask < {needed:.2f} needed", info

    return best_ask, "", "", info


def _list_candidate_markets(client: Any, cfg: Dict[str, Any], now: datetime) -> Iterable[Any]:
    """Lists open markets resolving inside the discovery horizon.

    Bounding on resolution time is what makes a 2-5 second cadence affordable:
    only rounds about to close come back, regardless of how many markets
    Polymarket is running. A tag id can be configured to narrow it further, but
    correctness does not depend on knowing one.
    """
    horizon = crypto_markets.discovery_horizon(
        cfg["entry_window_seconds"], cfg["discovery_lookahead_seconds"]
    )
    query: Dict[str, Any] = {
        "closed": False,
        "end_date_min": now,
        "end_date_max": now + horizon,
        "order": "endDate",
        "ascending": True,
        "page_size": cfg["discovery_page_size"],
    }
    tag_id = cfg.get("discovery_tag_id")
    if tag_id:
        query["tag_id"] = int(tag_id)

    pages = client.list_markets(**query)
    seen_pages = 0
    for page in pages:
        seen_pages += 1
        items = getattr(page, "items", None) or []
        if not items:
            break
        for market in items:
            yield market
        if seen_pages >= cfg["discovery_max_pages"]:
            break


def find_crypto_opportunities(
    client: Any = None,
    settings_override: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    claimed_round_keys: Optional[Set[str]] = None,
    held_token_ids: Optional[Set[str]] = None,
    log: Optional[Callable[[str, str], None]] = None,
) -> CryptoScanResult:
    """One crypto scan pass.

    Returns every side of every live round that passes asset, duration, status,
    entry-window, probability and order-book checks -- plus a structured record
    of everything that was refused and why.
    """
    global LAST_SCAN_ERROR
    LAST_SCAN_ERROR = None

    cfg = crypto_settings(settings_override)
    now = now or crypto_markets.utc_now()
    claimed_round_keys = claimed_round_keys or set()
    held_token_ids = held_token_ids or set()
    result = CryptoScanResult()

    allowed = crypto_markets.selected_symbols(cfg["assets"])
    if not allowed:
        result.error = "no approved crypto assets selected"
        LAST_SCAN_ERROR = result.error
        return result

    client = client or polymarket_client.get_public_client()

    try:
        for market in _list_candidate_markets(client, cfg, now):
            try:
                round_ = crypto_markets.classify_market(
                    market,
                    allowed_symbols=allowed,
                    expected_duration_seconds=cfg["round_duration_seconds"],
                    duration_tolerance_seconds=cfg["round_duration_tolerance_seconds"],
                )
            except MarketRejected as rejected:
                # Most markets in the horizon are simply not ours (wrong asset,
                # wrong shape, wrong duration). Those are recorded but not
                # logged individually, or a 3-second poll would drown the log.
                result.skipped.append(SkippedMarket(
                    reason=rejected.reason,
                    detail=rejected.detail,
                    slug=str(getattr(market, "slug", "") or ""),
                    market_id=str(getattr(market, "id", "") or ""),
                ))
                continue

            result.rounds_seen += 1
            try:
                result.skipped.extend(_evaluate_round(
                    client, market, round_, cfg, now, claimed_round_keys,
                    held_token_ids, result, log,
                ))
            except Exception as exc:
                # One malformed round must not cost the whole pass: with a
                # 30-second window, abandoning the scan means missing every
                # other asset's entry too.
                entry = SkippedMarket(
                    reason="evaluation_error",
                    detail=f"{type(exc).__name__}: {exc}",
                    asset=round_.asset, slug=round_.slug, market_id=round_.market_id,
                )
                result.skipped.append(entry)
                if log:
                    log(f"[crypto] SKIP {entry.describe()}", entry.throttle_key)

    except (RateLimitError, PolymarketError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive, mirrors scanner.py
        result.error = f"{type(exc).__name__}: {exc}"

    if result.error:
        LAST_SCAN_ERROR = result.error
        if log:
            log(f"[crypto] scan failed: {result.error}", "scan_failed")

    # Most urgent round first: with a 30-second window and several assets in
    # flight, the one closest to resolution is the one that will be lost.
    result.opportunities.sort(key=lambda o: (o.seconds_remaining, -o.confirmed_price))
    return result




def _evaluate_round(
    client: Any,
    market: Any,
    round_: CryptoRound,
    cfg: Dict[str, Any],
    now: datetime,
    claimed_round_keys: Set[str],
    held_token_ids: Set[str],
    result: CryptoScanResult,
    log: Optional[Callable[[str, str], None]],
) -> List[SkippedMarket]:
    """Applies the live / timing / price / order-book gates to one classified
    round, appending any qualifying side to `result.opportunities`.

    Up and Down are judged independently against the same floor: only a side
    that clears it on its own executable ask becomes an opportunity, and both
    may be refused.
    """
    skipped: List[SkippedMarket] = []

    def _skip(reason: str, detail: str, side: str = "") -> None:
        entry = SkippedMarket(
            reason=reason, detail=detail, asset=round_.asset,
            slug=round_.slug, market_id=round_.market_id, side=side,
        )
        skipped.append(entry)
        if log:
            log(f"[crypto] SKIP {entry.describe()}", entry.throttle_key)

    live_ok, live_detail = crypto_markets.is_market_live(market)
    if not live_ok:
        _skip(REASON_NOT_LIVE, live_detail)
        return skipped

    if round_.round_key in claimed_round_keys:
        _skip(REASON_ALREADY_TRADED, f"round {round_.round_key} already has an entry")
        return skipped

    remaining = round_.seconds_remaining(now)
    window_ok, window_reason, window_detail = crypto_markets.entry_window_check(
        remaining, cfg["entry_window_seconds"]
    )
    if not window_ok:
        _skip(window_reason, window_detail)
        return skipped

    # Gamma's volume/liquidity figures are a coarse sanity floor here, not the
    # real liquidity gate. The live API returns volume=null on a round this
    # young (read as 0.0), so crypto_min_volume must stay at 0 or nothing ever
    # qualifies; the order book below does the actual work.
    if round_.volume < cfg["min_volume"]:
        _skip(REASON_BELOW_MIN_VOLUME, f"volume ${round_.volume:.0f} < ${cfg['min_volume']:.0f}")
        return skipped
    if round_.liquidity < cfg["min_liquidity"]:
        _skip(REASON_BELOW_MIN_LIQUIDITY, f"liquidity ${round_.liquidity:.0f} < ${cfg['min_liquidity']:.0f}")
        return skipped

    floor = cfg["min_probability"]
    ceiling = cfg["max_probability"]
    stake = cfg["stake_per_trade"]

    for side in (SIDE_UP, SIDE_DOWN):
        token_id = round_.side_tokens.get(side)
        if not token_id:
            continue
        if token_id in held_token_ids:
            _skip(REASON_DUPLICATE_POSITION, "this outcome token is already held", side=side)
            continue

        gamma_price = round_.side_prices.get(side)
        if gamma_price is not None and gamma_price < (floor - GAMMA_PREFILTER_MARGIN):
            # Far enough below the floor that no live book could close the gap;
            # skip the request rather than the check.
            _skip(
                REASON_PRICE_BELOW_THRESHOLD,
                f"quoted {gamma_price:.4f} is more than {GAMMA_PREFILTER_MARGIN:.2f} below the "
                f"{floor:.2f} floor; order book not polled",
                side=side,
            )
            continue

        try:
            order_book = client.get_order_book(token_id=token_id)
        except (RateLimitError, PolymarketError) as exc:
            _skip(REASON_NO_BOOK, f"order book unavailable ({type(exc).__name__}: {exc})", side=side)
            continue

        required_shares = (stake / gamma_price) if (gamma_price and gamma_price > 0) else (stake / max(floor, 1e-9))
        ask, book_reason, book_detail, book_info = evaluate_book(
            order_book,
            required_shares=required_shares,
            max_spread=cfg["max_spread"],
            max_quote_age_seconds=cfg["max_quote_age_seconds"],
            min_depth_multiple=cfg["min_ask_depth_multiple"],
            now=now,
        )
        if ask is None:
            _skip(book_reason, book_detail, side=side)
            continue

        if ask < floor:
            _skip(
                REASON_PRICE_BELOW_THRESHOLD,
                f"executable ask {ask:.4f} < {floor:.4f} floor",
                side=side,
            )
            continue
        if ask > ceiling:
            _skip(
                REASON_PRICE_ABOVE_CEILING,
                f"executable ask {ask:.4f} > {ceiling:.4f} ceiling (no profit left to pay for the risk)",
                side=side,
            )
            continue

        result.opportunities.append(CryptoOpportunity(
            market_id=round_.market_id,
            question=round_.question,
            slug=round_.slug,
            outcome_label=side.capitalize(),
            token_id=token_id,
            gamma_price=float(gamma_price) if gamma_price is not None else float(ask),
            confirmed_price=float(ask),
            volume=round_.volume,
            liquidity=round_.liquidity,
            end_date=round_.end.isoformat(),
            asset=round_.asset,
            side=side,
            round_key=round_.round_key,
            seconds_remaining=remaining,
            best_bid=book_info.get("best_bid"),
            ask_size=float(book_info.get("ask_size") or 0.0),
            duration_source=round_.duration_source,
        ))
        if log:
            log(
                f"[crypto] SIGNAL {round_.asset} {side} {round_.slug or round_.market_id}: "
                f"ask {ask:.4f} >= {floor:.2f}, {remaining:.1f}s left, "
                f"{float(book_info.get('ask_size') or 0.0):.2f} shares at the ask",
                f"{round_.round_key}:{side}:signal",
            )

    return skipped
