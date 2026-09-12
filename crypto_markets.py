"""Classification and timing rules for Polymarket's fixed 5-minute
"Up or Down" crypto rounds.

This module is deliberately free of network calls and of any dependency on the
sports strategy: everything here is a pure function over a market object (or a
lightweight stand-in with the same attributes), so the qualification rules can
be tested exhaustively without hitting Gamma or the CLOB.

Why a dedicated module instead of extending scanner.py:

* Sports late-game timing infers "the game is nearly over" from a resolution
  deadline plus a confirmed kickoff, because a match has no fixed length. That
  heuristic must not change, and crypto must not inherit it.
* These crypto markets ARE fixed length by construction -- a round opens and
  closes on a five-minute boundary -- so the round-end timestamp is
  authoritative and can be used directly. That is only sound *because* the
  duration is verified to be five minutes first, which is what
  `classify_market` does before anything else is allowed to trade.

Identification uses structural metadata rather than loose title matching:

* the two tradable outcomes must be labelled exactly "Up" and "Down";
* the asset and the round length come from the canonical slug, which Polymarket
  emits in the machine-readable form `{asset}-updown-{duration}-{startEpoch}`
  (e.g. `btc-updown-5m-1789214400`);
* that slug is cross-checked against the round-end timestamp: the API's end date
  must equal the slug's start epoch plus the slug's stated duration.

DO NOT use `market.state.start_date` to measure one of these rounds. On the live
API it is the *listing* time, roughly 24 hours before the round it belongs to --
`btc-updown-5m-1789214400` is published with startDate 2026-09-11T12:09:37Z and
endDate 2026-09-12T12:05:00Z. Measuring end minus start there yields ~86,000
seconds and would reject every genuine 5-minute round. The round's true start is
the epoch in the slug (Gamma also carries it as `eventStartTime`, but the SDK's
Market model does not surface that field). This is the single most important
invariant in this module; the cross-check above is what enforces it.

Anything that fails one of those is rejected with a machine-readable reason so
the caller can log exactly why a market was passed over.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

# --- Reject reasons -------------------------------------------------------
# Stable string codes so logs, tests and the dashboard all agree on the
# vocabulary for "why was this market skipped".
REASON_NO_OUTCOMES = "no_outcomes"
REASON_NOT_UP_DOWN = "not_up_down_market"
REASON_MISSING_TOKEN = "missing_token_id"
REASON_UNKNOWN_ASSET = "unapproved_asset"
REASON_AMBIGUOUS_ASSET = "ambiguous_asset"
REASON_ASSET_NOT_SELECTED = "asset_not_selected"
REASON_NO_END_TIMESTAMP = "no_round_end_timestamp"
REASON_AMBIGUOUS_DURATION = "ambiguous_round_duration"
REASON_WRONG_DURATION = "wrong_round_duration"
REASON_NOT_LIVE = "not_live"
REASON_EXPIRED = "round_expired"
REASON_TOO_EARLY = "outside_entry_window"
REASON_PRICE_BELOW_THRESHOLD = "price_below_threshold"
REASON_PRICE_ABOVE_CEILING = "price_above_ceiling"
REASON_BELOW_MIN_VOLUME = "below_min_volume"
REASON_BELOW_MIN_LIQUIDITY = "below_min_liquidity"
REASON_NO_BOOK = "no_executable_book"
REASON_BOOK_NO_ASKS = "book_no_asks"      # nothing offered: there is nothing to buy
REASON_BOOK_NO_BIDS = "book_no_bids"      # offered, but no resting bid to price against
REASON_BOOK_ONE_SIDED = "book_one_sided"  # retained for callers matching on the old code
REASON_BOOK_WIDE_SPREAD = "book_spread_too_wide"
REASON_BOOK_STALE = "book_quote_stale"
REASON_BOOK_THIN = "book_depth_insufficient"
REASON_ALREADY_TRADED = "already_traded_this_round"
REASON_DUPLICATE_POSITION = "duplicate_position"
REASON_RISK_BLOCKED = "risk_limit"
REASON_SLIPPAGE = "slippage"
REASON_NET_EDGE = "net_edge_below_floor"
REASON_OFF_TICK = "price_off_tick"

SIDE_UP = "UP"
SIDE_DOWN = "DOWN"

# The canonical length of these rounds. Kept as a constant rather than a magic
# number so the duration gate and the settings default cannot drift apart.
ROUND_DURATION_SECONDS = 300


@dataclass(frozen=True)
class CryptoAsset:
    """One approved underlying.

    `slug_tokens` are matched as whole hyphen-separated slug segments, never as
    substrings: "eth" must not match "ethereum-classic-...", and "sol" must not
    match "solana" only by accident of both being listed.
    """

    symbol: str
    name: str
    slug_tokens: Tuple[str, ...]


# The approved universe. Nothing outside this table is ever tradable by the
# crypto strategy, whatever the settings file says.
APPROVED_ASSETS: Tuple[CryptoAsset, ...] = (
    CryptoAsset("BTC", "Bitcoin", ("btc", "bitcoin", "xbt")),
    CryptoAsset("ETH", "Ethereum", ("eth", "ethereum", "ether")),
    CryptoAsset("SOL", "Solana", ("sol", "solana")),
    CryptoAsset("XRP", "XRP", ("xrp", "ripple")),
    CryptoAsset("DOGE", "Dogecoin", ("doge", "dogecoin")),
)

ASSETS_BY_SYMBOL: Dict[str, CryptoAsset] = {a.symbol: a for a in APPROVED_ASSETS}
_TOKEN_TO_SYMBOL: Dict[str, str] = {
    token: asset.symbol for asset in APPROVED_ASSETS for token in asset.slug_tokens
}

# Slug segments that mark a five-minute cadence, used only as a fallback when
# Gamma did not hydrate a start timestamp to measure the round against.
_FIVE_MINUTE_SLUG_MARKERS = frozenset({"5m", "5min", "5mins", "5minute", "5minutes"})
_MINUTE_WORDS = frozenset({"m", "min", "mins", "minute", "minutes"})

_SLUG_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Polymarket's canonical slug for these rounds: asset, round length and the
# round's start epoch, e.g. "btc-updown-5m-1789214400" (a 15-minute round of the
# same asset is "btc-updown-15m-...", which this parses and the duration gate
# then rejects).
_CANONICAL_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-updown-(?P<count>\d{1,4})(?P<unit>min|m|h|d)-(?P<start>\d{9,12})$"
)
_UNIT_SECONDS = {"m": 60.0, "min": 60.0, "h": 3600.0, "d": 86400.0}

# How far the API's round-end timestamp may sit from the slug's own
# start + duration before the two are treated as disagreeing. These are minted on
# exact boundaries, so any real drift here means the slug was misread.
SLUG_SCHEDULE_TOLERANCE_SECONDS = 5.0


@dataclass(frozen=True)
class CanonicalSlug:
    """The schedule Polymarket encodes in a round's slug."""

    asset_token: str
    duration_seconds: float
    round_start: datetime


def parse_canonical_slug(slug: Optional[str]) -> Optional[CanonicalSlug]:
    """Parses `{asset}-updown-{duration}-{startEpoch}`, or None if it isn't one."""
    match = _CANONICAL_SLUG_RE.match((slug or "").strip().lower())
    if match is None:
        return None
    unit_seconds = _UNIT_SECONDS.get(match.group("unit"))
    if unit_seconds is None:
        return None
    try:
        start = datetime.fromtimestamp(int(match.group("start")), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return CanonicalSlug(
        asset_token=match.group("asset"),
        duration_seconds=int(match.group("count")) * unit_seconds,
        round_start=start,
    )


class MarketRejected(Exception):
    """Raised by classify_market when a market is not a tradable crypto round."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class CryptoRound:
    """A validated live 5-minute Up/Down round for one approved asset."""

    asset: str
    market_id: str
    slug: str
    question: str
    end: datetime
    # The round's true start, taken from the slug's epoch -- never from
    # market.state.start_date, which is the listing time (see the module
    # docstring). None only on the degraded fallback path.
    round_start: Optional[datetime]
    duration_seconds: float
    duration_source: str          # "slug_schedule" | "slug_marker"
    side_tokens: Dict[str, str]   # {"UP": token_id, "DOWN": token_id}
    side_prices: Dict[str, Optional[float]]
    volume: float
    liquidity: float

    @property
    def round_key(self) -> str:
        """Identity of this market/round for the duplicate-entry ledger.

        The market id alone would be enough today, but pinning the round end
        into the key means a recycled or re-dated market can never inherit a
        previous round's "already traded" flag, and vice versa.
        """
        return f"crypto:{self.asset}:{self.market_id}:{int(self.end.timestamp())}"

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        return seconds_until(self.end, now)


def normalize_symbol(value: str) -> str:
    """Maps any accepted spelling of an asset ("bitcoin", "btc", "BTC") to its
    canonical symbol, or "" when it is not an approved asset."""
    token = (value or "").strip().lower()
    return _TOKEN_TO_SYMBOL.get(token, "")


def selected_symbols(configured: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Resolves the configured asset list to canonical, approved symbols.

    Unknown entries are dropped rather than trusted; an empty or absent list
    means "every approved asset". Order follows APPROVED_ASSETS so behaviour
    does not depend on how the settings file happens to be ordered.
    """
    if configured is None:
        return tuple(a.symbol for a in APPROVED_ASSETS)
    wanted = {normalize_symbol(str(v)) for v in configured}
    wanted.discard("")
    if not wanted:
        return ()
    return tuple(a.symbol for a in APPROVED_ASSETS if a.symbol in wanted)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_until(end: Optional[datetime], now: Optional[datetime] = None) -> float:
    """Seconds from `now` until `end`. Negative once the round has closed."""
    end_utc = _as_utc(end)
    if end_utc is None:
        raise ValueError("seconds_until requires an end timestamp")
    return (end_utc - _as_utc(now or utc_now())).total_seconds()


def slug_segments(*values: Optional[str]) -> Tuple[str, ...]:
    """Splits slugs (and, as a fallback, free text) into lowercase segments."""
    out = []
    for value in values:
        if not value:
            continue
        for part in _SLUG_SPLIT_RE.split(str(value).lower()):
            if part:
                out.append(part)
    return tuple(out)


def detect_asset(segments: Sequence[str]) -> Tuple[str, str]:
    """Returns (symbol, reason). reason is "" on success.

    A round naming two different approved assets is rejected as ambiguous
    rather than guessed at -- entering the wrong underlying is worse than
    missing a round.
    """
    found = []
    for seg in segments:
        symbol = _TOKEN_TO_SYMBOL.get(seg)
        if symbol and symbol not in found:
            found.append(symbol)
    if not found:
        return "", REASON_UNKNOWN_ASSET
    if len(found) > 1:
        return "", REASON_AMBIGUOUS_ASSET
    return found[0], ""


def has_five_minute_marker(segments: Sequence[str]) -> bool:
    """True when the slug explicitly advertises a 5-minute cadence."""
    for i, seg in enumerate(segments):
        if seg in _FIVE_MINUTE_SLUG_MARKERS:
            return True
        if seg == "5" and i + 1 < len(segments) and segments[i + 1] in _MINUTE_WORDS:
            return True
    return False


def _outcome_sides(market: Any) -> Dict[str, Any]:
    """Maps the market's two outcomes onto UP/DOWN by their labels.

    The labels are the structural signal that this is an Up/Down market: a
    market whose outcomes are Yes/No, or Over/Under, is not one of these rounds
    no matter what its title says.
    """
    outcomes = getattr(market, "outcomes", None)
    if outcomes is None:
        raise MarketRejected(REASON_NO_OUTCOMES, "market has no outcomes")

    sides: Dict[str, Any] = {}
    for attr in ("yes", "no"):
        outcome = getattr(outcomes, attr, None)
        if outcome is None:
            continue
        label = str(getattr(outcome, "label", "") or "").strip().lower()
        if label == "up":
            sides[SIDE_UP] = outcome
        elif label == "down":
            sides[SIDE_DOWN] = outcome

    if SIDE_UP not in sides or SIDE_DOWN not in sides:
        raise MarketRejected(
            REASON_NOT_UP_DOWN,
            "outcome labels are not exactly Up/Down",
        )
    return sides


def _resolve_schedule(
    slug: Optional[str],
    end: datetime,
    segments: Sequence[str],
    tolerance_seconds: float,
) -> Tuple[float, Optional[datetime], str]:
    """Determines the round's length and true start, and where they came from.

    Returns (duration_seconds, round_start, source).

    The canonical slug is the authority: it carries the round's length and its
    start epoch, and those are cross-checked against the API's own end date. If
    the two disagree the slug was misread and the round is refused rather than
    traded on a guess.

    `market.state.start_date` is deliberately never consulted -- on the live API
    it is the listing time, about a day before the round (see the module
    docstring), so measuring a round against it rejects every genuine one.
    """
    parsed = parse_canonical_slug(slug)
    if parsed is not None:
        implied_end = parsed.round_start + timedelta(seconds=parsed.duration_seconds)
        drift = abs((end - implied_end).total_seconds())
        if drift > tolerance_seconds:
            raise MarketRejected(
                REASON_WRONG_DURATION,
                f"slug schedule disagrees with the round end: slug says "
                f"{parsed.round_start.isoformat()} + {parsed.duration_seconds:.0f}s, "
                f"API says {end.isoformat()} ({drift:.0f}s apart)",
            )
        return parsed.duration_seconds, parsed.round_start, "slug_schedule"

    # Degraded fallback for a slug shape this parser does not know: accept an
    # explicit 5-minute marker, and derive the start from the end. Without one
    # the round length is unknown and must not be assumed.
    if has_five_minute_marker(segments):
        duration = float(ROUND_DURATION_SECONDS)
        return duration, end - timedelta(seconds=duration), "slug_marker"

    raise MarketRejected(
        REASON_AMBIGUOUS_DURATION,
        "slug is not in the canonical {asset}-updown-{duration}-{startEpoch} form "
        "and carries no explicit 5-minute marker",
    )


# --- Trading constraints and fees -------------------------------------------
# Both are modelled fields on Market.trading, so they are read from the market
# itself rather than assumed. Per the docs: "Always read the active value from
# the market rather than assuming a fixed increment."


@dataclass(frozen=True)
class FeeTerms:
    """A market's taker fee schedule, as published on the market."""

    enabled: bool = False
    rate: float = 0.0
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.0

    def taker_fee_per_share(self, price: float) -> float:
        """USDC charged per share to the taker at `price`.

        Polymarket's published formula is `fee = C x feeRate x p x (1 - p)`,
        with the schedule's exponent applied to the price component. The fee is
        symmetric about 0.50, so a fill at 0.95 costs the same as one at 0.05.
        Makers are never charged.
        """
        if not self.enabled or self.rate <= 0:
            return 0.0
        p = max(0.0, min(1.0, float(price)))
        component = p * (1.0 - p)
        try:
            component = component ** float(self.exponent)
        except (ValueError, OverflowError):
            return 0.0
        return float(self.rate) * component


@dataclass(frozen=True)
class TradingConstraints:
    """The market's own price grid and minimum order size."""

    tick_size: Optional[float] = None
    min_order_size: Optional[float] = None


def read_fee_terms(market: Any) -> FeeTerms:
    trading = getattr(market, "trading", None)
    schedule = getattr(trading, "fee_schedule", None) if trading else None
    enabled = bool(getattr(trading, "fees_enabled", False)) if trading else False
    if schedule is None:
        return FeeTerms(enabled=False)
    return FeeTerms(
        enabled=enabled,
        rate=float(getattr(schedule, "rate", 0.0) or 0.0),
        exponent=float(getattr(schedule, "exponent", 1.0) or 1.0),
        taker_only=bool(getattr(schedule, "taker_only", True)),
        rebate_rate=float(getattr(schedule, "rebate_rate", 0.0) or 0.0),
    )


def read_trading_constraints(market: Any) -> TradingConstraints:
    trading = getattr(market, "trading", None)
    if trading is None:
        return TradingConstraints()
    tick = getattr(trading, "minimum_tick_size", None)
    size = getattr(trading, "minimum_order_size", None)
    return TradingConstraints(
        tick_size=float(tick) if tick is not None else None,
        min_order_size=float(size) if size is not None else None,
    )


def net_edge_per_share(price: float, fee_terms: FeeTerms) -> float:
    """What one share actually returns if this side wins, after the taker fee.

    Buying at `price` pays out 1.00 on a win, so the gross edge is (1 - price);
    the taker fee is charged at match time whichever way the round resolves.
    """
    return (1.0 - float(price)) - fee_terms.taker_fee_per_share(price)


def price_on_tick(price: float, tick_size: Optional[float]) -> bool:
    """Whether `price` sits on the market's price grid.

    An order priced off the grid is rejected by the exchange, so this is checked
    before submitting rather than discovered from a rejection.
    """
    if not tick_size or tick_size <= 0:
        return True
    steps = float(price) / float(tick_size)
    return abs(steps - round(steps)) < 1e-6


def round_down_to_tick(price: float, tick_size: Optional[float]) -> float:
    """Snaps a buy price DOWN to the grid -- never up, which would pay more.

    Uses Decimal for the arithmetic: binary floats turn 0.957 on a 0.01 grid
    into 0.9500000000000001, which is off-grid and would be rejected.
    """
    if not tick_size or tick_size <= 0:
        return float(price)
    tick = Decimal(str(tick_size))
    steps = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_FLOOR)
    return float(steps * tick)


def is_market_live(market: Any) -> Tuple[bool, str]:
    """Tradability of the market itself, independent of timing."""
    state = getattr(market, "state", None)
    if state is None:
        return False, "market has no state block"
    if getattr(state, "closed", False):
        return False, "market is closed"
    if getattr(state, "archived", False):
        return False, "market is archived"
    if getattr(state, "active", None) is False:
        return False, "market is not active"
    if getattr(state, "accepting_orders", None) is False:
        return False, "market is not accepting orders"
    return True, ""


def classify_market(
    market: Any,
    allowed_symbols: Optional[Iterable[str]] = None,
    expected_duration_seconds: float = ROUND_DURATION_SECONDS,
    duration_tolerance_seconds: float = 20.0,
) -> CryptoRound:
    """Validates one market as a live 5-minute Up/Down round for an approved
    asset, or raises MarketRejected carrying the reason code.

    This performs no timing-window check: `CryptoRound.seconds_remaining` and
    `entry_window_check` do that, so the caller can log "qualified but too
    early" distinctly from "not one of our markets at all".
    """
    sides = _outcome_sides(market)

    side_tokens: Dict[str, str] = {}
    side_prices: Dict[str, Optional[float]] = {}
    for side, outcome in sides.items():
        token_id = getattr(outcome, "token_id", None)
        if not token_id:
            raise MarketRejected(REASON_MISSING_TOKEN, f"{side} outcome has no token id")
        side_tokens[side] = str(token_id)
        raw_price = getattr(outcome, "price", None)
        side_prices[side] = float(raw_price) if raw_price is not None else None

    slug = str(getattr(market, "slug", "") or "")
    question = str(getattr(market, "question", "") or "") or slug
    event_slugs = [getattr(e, "slug", None) for e in (getattr(market, "events", None) or ())]
    segments = slug_segments(slug, *event_slugs)

    # The canonical slug names the asset in one exact position, so when it parses
    # there is nothing to infer: an unapproved coin there is unapproved, full stop.
    canonical = parse_canonical_slug(slug)
    if canonical is not None:
        asset = normalize_symbol(canonical.asset_token)
        if not asset:
            raise MarketRejected(
                REASON_UNKNOWN_ASSET,
                f"{canonical.asset_token!r} is not one of the approved assets",
            )
    else:
        asset, reason = detect_asset(segments)
        if not asset:
            # Only fall back to the question once the slugs have come up empty,
            # and only on whole words, so "MATIC up or down" can't read as "TIC".
            asset, reason = detect_asset(slug_segments(question))
        if not asset:
            raise MarketRejected(reason, f"no approved asset in {slug or question!r}")

    allowed = selected_symbols(allowed_symbols) if allowed_symbols is not None else None
    if allowed is not None and asset not in allowed:
        raise MarketRejected(REASON_ASSET_NOT_SELECTED, f"{asset} is not in the selected asset list")

    state = getattr(market, "state", None)
    end = _as_utc(getattr(state, "end_date", None) if state else None)
    if end is None:
        raise MarketRejected(REASON_NO_END_TIMESTAMP, "market has no end timestamp")

    duration, round_start, duration_source = _resolve_schedule(
        slug, end, segments, SLUG_SCHEDULE_TOLERANCE_SECONDS
    )
    if abs(duration - expected_duration_seconds) > duration_tolerance_seconds:
        raise MarketRejected(
            REASON_WRONG_DURATION,
            f"round is {duration:.0f}s long, expected {expected_duration_seconds:.0f}s "
            f"(+/-{duration_tolerance_seconds:.0f}s, from {duration_source})",
        )

    metrics = getattr(market, "metrics", None)
    volume = float(getattr(metrics, "volume", None) or 0.0) if metrics else 0.0
    liquidity = float(getattr(metrics, "liquidity", None) or 0.0) if metrics else 0.0

    return CryptoRound(
        asset=asset,
        market_id=str(getattr(market, "id", "")),
        slug=slug,
        question=question,
        end=end,
        round_start=round_start,
        duration_seconds=duration,
        duration_source=duration_source,
        side_tokens=side_tokens,
        side_prices=side_prices,
        volume=volume,
        liquidity=liquidity,
    )


def entry_window_check(seconds_remaining: float, window_seconds: float) -> Tuple[bool, str, str]:
    """The entry-timing gate: 0 < seconds_remaining <= window_seconds.

    Returns (ok, reason_code, detail). The bounds are deliberately asymmetric:
    a round with exactly `window_seconds` left is the first tick of the window
    and tradable, while one with exactly 0s left has already resolved and is
    not. Anything non-finite is treated as ambiguous and refused.
    """
    try:
        remaining = float(seconds_remaining)
    except (TypeError, ValueError):
        return False, REASON_NO_END_TIMESTAMP, "seconds remaining is not a number"
    if remaining != remaining or remaining in (float("inf"), float("-inf")):  # NaN / inf
        return False, REASON_NO_END_TIMESTAMP, "seconds remaining is not finite"
    if remaining <= 0:
        return False, REASON_EXPIRED, f"round closed {abs(remaining):.1f}s ago"
    if remaining > window_seconds:
        return False, REASON_TOO_EARLY, (
            f"{remaining:.1f}s left, entry window opens at {window_seconds:.0f}s"
        )
    return True, "", f"{remaining:.1f}s left (window {window_seconds:.0f}s)"


def discovery_horizon(window_seconds: float, lookahead_seconds: float) -> timedelta:
    """How far ahead of the entry window to list markets.

    Discovery must see a round *before* its window opens, or the first poll
    inside a 30-second window would be spent finding it rather than trading it.
    """
    return timedelta(seconds=max(float(window_seconds), 0.0) + max(float(lookahead_seconds), 0.0))
