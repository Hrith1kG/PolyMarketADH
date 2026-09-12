"""Sport-aware "how long until this match actually ends?" estimation.

Answers one question for a live Polymarket event: how many WALL-CLOCK minutes of real
time are left before the match finishes -- stoppages, intermissions and broadcast
overhead included. That is what "late game" means to a trader, and it is the only
quantity the Late Game strategy gates on.

`end_date` is deliberately never read. It is `start_time` for soccer and college
football, `start + 6h` for esports and `start + 7 days` for tennis, baseball and
cricket, so it carries no information about when a game finishes. See
docs/LATE_GAME_TIMING.md for the sampled evidence behind every rule here.

Only three in-play fields exist on the live payload, all untyped strings:
`period` (nearly always set), `elapsed` (soccer only, whole minutes) and `score`.
Each sport is therefore handled by the most reliable signal it actually publishes,
and a sport whose state yields no dependable estimate is skipped with a reason rather
than guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# --- Outcome of an estimate -------------------------------------------------------

BASIS_CLOCK = "clock"          # a real in-play game clock was published
BASIS_PERIOD = "period_worst_case"  # period known, no clock: assume all of it remains
BASIS_SERIES = "series"        # best-of-N series position

# Reasons an event yields no usable estimate. Kept as constants so callers can branch
# on them and tests can assert on them without matching prose.
REASON_NOT_LIVE = "not_live"
REASON_SPORT_UNSUPPORTED = "sport_unsupported"
REASON_TIMING_UNAVAILABLE = "timing_unavailable"


@dataclass(frozen=True)
class TimingEstimate:
    """Either an estimate (`remaining_minutes` set) or a skip (`reason` set)."""
    sport: str
    remaining_minutes: Optional[float] = None
    full_match_minutes: Optional[float] = None
    basis: Optional[str] = None
    reason: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.remaining_minutes is not None

    def describe(self) -> str:
        if self.ok:
            return (f"{self.sport}: ~{self.remaining_minutes:.0f} min remaining "
                    f"({self.basis}; {self.detail})")
        return f"{self.sport}: {self.reason} ({self.detail})"


# --- Period vocabulary ------------------------------------------------------------
#
# Untyped strings with no documented vocabulary, so these sets are built from values
# actually observed on the live feed plus the standard OpticOdds/Sportradar codes.
# Anything unrecognised is skipped and logged by name rather than assumed to be late.

PERIODS_FINISHED = {"VFT", "FT", "AOT", "AP", "AET", "FINAL", "ENDED", "OVER"}
PERIODS_NOT_STARTED = {"NS", "PRE", "SCHEDULED", "TBD"}
PERIODS_NOT_PLAYING = {"SUS", "POSTP", "CANC", "ABD", "INT", "DELAY", "DELAYED", "RAIN"}

_SERIES_PERIOD_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_SERIES_SCORE_RE = re.compile(r"\|\s*(\d+)\s*[-:]\s*(\d+)\s*\|\s*[Bb][Oo]\s*(\d+)")
_QUARTER_RE = re.compile(r"^\s*(?:Q\s*(\d)|(\d)\s*Q)\s*$", re.I)
_HALF_RE = re.compile(r"^\s*(?:H\s*(\d)|(\d)\s*H)\s*$", re.I)
_HOCKEY_PERIOD_RE = re.compile(r"^\s*(?:P\s*(\d)|(\d)\s*P)\s*$", re.I)
_OVERTIME_RE = re.compile(r"^\s*(?:OT|ET\d?|SO|PEN)\d*\s*$", re.I)


def _norm_period(period: Optional[str]) -> str:
    return (period or "").strip().upper()


# --- Sport rules ------------------------------------------------------------------

FAMILY_CLOCK_SOCCER = "soccer"
FAMILY_PERIOD = "period"
FAMILY_SERIES = "series"
FAMILY_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SportRule:
    """How one sport (or sport family) converts live state into remaining wall time.

    `full_match_minutes` is the typical end-to-end wall duration, used for the
    proportional half of the eligibility limit so that "late game" scales with how
    long the format actually runs.
    """
    family: str
    full_match_minutes: float
    # PERIOD sports
    period_count: int = 0
    period_wall_minutes: float = 0.0
    intermission_minutes: float = 0.0
    intermissions_remaining_after: int = 0  # breaks left once the final period starts
    overtime_wall_minutes: float = 0.0
    # SERIES (esports) sports
    map_wall_minutes: float = 0.0
    # UNSUPPORTED sports
    unsupported_detail: str = ""


# Regulation 90 min, 15 min half-time, and an in-play stretch factor for stoppage
# time, VAR checks and substitutions -- a "90 minute" match runs ~115 wall minutes.
SOCCER_REGULATION_MINUTES = 90.0
SOCCER_HALF_MINUTES = 45.0
SOCCER_HALFTIME_MINUTES = 15.0
SOCCER_STOPPAGE_STRETCH = 1.15
SOCCER_EXTRA_TIME_MINUTES = 30.0
SOCCER_PENALTIES_MINUTES = 12.0
SOCCER_FULL_MATCH_MINUTES = 115.0

# Sports whose family cannot be inferred from tags: their `sport.tags` carry only
# their own league tag, so they are named explicitly.
SPORT_RULES: Dict[str, SportRule] = {
    "nba": SportRule(FAMILY_PERIOD, 140.0, period_count=4, period_wall_minutes=30.0,
                     intermission_minutes=15.0, intermissions_remaining_after=0,
                     overtime_wall_minutes=12.0),
    "wnba": SportRule(FAMILY_PERIOD, 120.0, period_count=4, period_wall_minutes=26.0,
                      intermission_minutes=15.0, overtime_wall_minutes=10.0),
    "ncaab": SportRule(FAMILY_PERIOD, 120.0, period_count=2, period_wall_minutes=50.0,
                       intermission_minutes=15.0, overtime_wall_minutes=12.0),
    "nfl": SportRule(FAMILY_PERIOD, 190.0, period_count=4, period_wall_minutes=42.0,
                     intermission_minutes=13.0, overtime_wall_minutes=20.0),
    "cfb": SportRule(FAMILY_PERIOD, 210.0, period_count=4, period_wall_minutes=47.0,
                     intermission_minutes=20.0, overtime_wall_minutes=20.0),
    "ncaaf": SportRule(FAMILY_PERIOD, 210.0, period_count=4, period_wall_minutes=47.0,
                       intermission_minutes=20.0, overtime_wall_minutes=20.0),
    "nhl": SportRule(FAMILY_PERIOD, 150.0, period_count=3, period_wall_minutes=40.0,
                     intermission_minutes=18.0, overtime_wall_minutes=15.0),
    "mlb": SportRule(FAMILY_UNSUPPORTED, 180.0,
                     unsupported_detail="baseball publishes no in-play inning field"),
}

# Per-title esports map durations. Everything else on the esports family tag falls
# back to ESPORTS_DEFAULT_MAP_MINUTES rather than being rejected.
ESPORTS_DEFAULT_MAP_MINUTES = 35.0
ESPORTS_MAP_MINUTES: Dict[str, float] = {
    "cs2": 40.0,
    "csgo": 40.0,
    "dota2": 42.0,
    "val": 40.0,
    "valorant": 40.0,
    "r6siege": 35.0,
    "lol": 33.0,
    "mlbb": 22.0,
    "hok": 20.0,
    "ow": 25.0,
    "rl": 15.0,
}

# Gamma family tag ids, verified against /tags. These cover the long tail (every
# soccer league, every esports title) without enumerating each league code.
TAG_SOCCER = 100350
TAG_ESPORTS = 64
TAG_TENNIS = 864
TAG_CRICKET = 517

FAMILY_TAG_RULES: Dict[int, SportRule] = {
    TAG_SOCCER: SportRule(FAMILY_CLOCK_SOCCER, SOCCER_FULL_MATCH_MINUTES),
    TAG_ESPORTS: SportRule(FAMILY_SERIES, 90.0,
                           map_wall_minutes=ESPORTS_DEFAULT_MAP_MINUTES),
    TAG_TENNIS: SportRule(FAMILY_UNSUPPORTED, 100.0,
                          unsupported_detail="tennis has no bounded remaining duration"),
    TAG_CRICKET: SportRule(FAMILY_UNSUPPORTED, 480.0,
                           unsupported_detail="cricket exposes no in-play progress"),
}


def sport_code(event: Any) -> str:
    """Best available identifier for the sport, e.g. 'kor', 'nba', 'cs2'."""
    sports = getattr(event, "sports", None)
    if sports is not None:
        sport_obj = getattr(sports, "sport", None)
        code = getattr(sport_obj, "sport", None) if sport_obj is not None else None
        if code:
            return str(code).strip().lower()
        series = getattr(sports, "series_slug", None)
        if series:
            return str(series).strip().lower()
    return str(getattr(event, "subcategory", None)
               or getattr(event, "category", None) or "unknown").strip().lower()


def _tag_ids(event: Any) -> Tuple[int, ...]:
    """Tag ids for the event, read from the sport's tag string and the event's tags."""
    ids = []
    sports = getattr(event, "sports", None)
    sport_obj = getattr(sports, "sport", None) if sports is not None else None
    raw = getattr(sport_obj, "tags", None) if sport_obj is not None else None
    if isinstance(raw, str):
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    elif isinstance(raw, (list, tuple)):
        for part in raw:
            try:
                ids.append(int(part))
            except (TypeError, ValueError):
                continue
    for tag in (getattr(event, "tags", None) or ()):
        tag_id = getattr(tag, "id", tag)
        try:
            ids.append(int(tag_id))
        except (TypeError, ValueError):
            continue
    return tuple(ids)


def resolve_rule(event: Any) -> Optional[SportRule]:
    """Finds the timing rule for an event: explicit sport code first, then family tag.

    Returns None when neither layer matches, which the caller reports as
    `sport_unsupported` -- never as "probably nearly over".
    """
    code = sport_code(event)
    rule = SPORT_RULES.get(code)
    if rule is not None:
        return rule

    tags = _tag_ids(event)
    for tag_id in tags:
        family_rule = FAMILY_TAG_RULES.get(tag_id)
        if family_rule is None:
            continue
        if family_rule.family == FAMILY_SERIES:
            # Same family, per-title map length.
            return SportRule(
                FAMILY_SERIES,
                family_rule.full_match_minutes,
                map_wall_minutes=ESPORTS_MAP_MINUTES.get(code, ESPORTS_DEFAULT_MAP_MINUTES),
            )
        return family_rule
    return None


# --- Per-family estimators --------------------------------------------------------

def _estimate_soccer(code: str, period: str, elapsed: Optional[str]) -> TimingEstimate:
    """Soccer is the one family with a real published clock: `elapsed` in minutes."""
    full = SOCCER_FULL_MATCH_MINUTES

    if period in ("HT", "HALFTIME"):
        # Second half in full, plus whatever of the interval is left. The interval's
        # progress is not published, so assume all of it remains.
        remaining = SOCCER_HALFTIME_MINUTES + SOCCER_HALF_MINUTES * SOCCER_STOPPAGE_STRETCH
        return TimingEstimate(code, remaining, full, BASIS_CLOCK, detail="half-time")

    if _OVERTIME_RE.match(period):
        if period.startswith("PEN") or period.startswith("SO"):
            return TimingEstimate(code, SOCCER_PENALTIES_MINUTES, full, BASIS_CLOCK,
                                  detail="penalty shootout")
        played = _parse_int(elapsed)
        if played is None:
            return TimingEstimate(code, SOCCER_EXTRA_TIME_MINUTES, full, BASIS_CLOCK,
                                  detail="extra time, clock unavailable")
        left = max(0.0, (SOCCER_REGULATION_MINUTES + SOCCER_EXTRA_TIME_MINUTES) - played)
        return TimingEstimate(code, left * SOCCER_STOPPAGE_STRETCH, full, BASIS_CLOCK,
                              detail=f"extra time, {played}'")

    played = _parse_int(elapsed)
    if played is None:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"period={period!r} but elapsed={elapsed!r} is not a minute count")

    if period in ("1H", "H1", "1"):
        game_left = (SOCCER_HALF_MINUTES - played) + SOCCER_HALF_MINUTES
        remaining = game_left * SOCCER_STOPPAGE_STRETCH + SOCCER_HALFTIME_MINUTES
        return TimingEstimate(code, max(remaining, 0.0), full, BASIS_CLOCK,
                              detail=f"first half, {played}'")

    if period in ("2H", "H2", "2"):
        game_left = SOCCER_REGULATION_MINUTES - played
        return TimingEstimate(code, max(game_left, 0.0) * SOCCER_STOPPAGE_STRETCH, full,
                              BASIS_CLOCK, detail=f"second half, {played}'")

    return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                          detail=f"unrecognised soccer period {period!r}")


def _estimate_period_sport(code: str, rule: SportRule, period: str,
                           allow_worst_case: bool) -> TimingEstimate:
    """Quarters/halves/periods with no in-period clock.

    Nothing published says how far into the current period play has got, so the whole
    of it is assumed to remain. Conservative by construction: it can only ever
    overstate the time left, which delays entry rather than triggering it early.
    """
    full = rule.full_match_minutes
    if not allow_worst_case:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"{code} has no in-period clock and worst-case estimation is disabled")

    if _OVERTIME_RE.match(period):
        return TimingEstimate(code, rule.overtime_wall_minutes or rule.period_wall_minutes,
                              full, BASIS_PERIOD, detail="overtime")

    current = _parse_period_number(period, rule)
    if current is None:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"unrecognised {code} period {period!r}")
    if current > rule.period_count:
        # Beyond regulation without matching the overtime pattern.
        return TimingEstimate(code, rule.overtime_wall_minutes or rule.period_wall_minutes,
                              full, BASIS_PERIOD, detail=f"period {current} (past regulation)")

    periods_left = rule.period_count - current + 1
    remaining = periods_left * rule.period_wall_minutes
    # Intermissions still to come: one before each period after the current one.
    remaining += max(0, periods_left - 1) * rule.intermission_minutes
    return TimingEstimate(code, remaining, full, BASIS_PERIOD,
                          detail=f"period {current}/{rule.period_count}, whole period assumed remaining")


def _estimate_series(code: str, rule: SportRule, period: str,
                     score: Optional[str]) -> TimingEstimate:
    """Best-of-N esports: remaining maps x that title's typical map duration.

    Maps left come from the SERIES score where it is published, not the map number:
    a Bo3 standing at 1-1 is decided by the current map alone, whatever `period` says.
    """
    map_minutes = rule.map_wall_minutes or ESPORTS_DEFAULT_MAP_MINUTES

    best_of = None
    wins_a = wins_b = None
    if score:
        match = _SERIES_SCORE_RE.search(score)
        if match:
            wins_a, wins_b, best_of = (int(match.group(1)), int(match.group(2)),
                                       int(match.group(3)))

    current = maps_in_series = None
    period_match = _SERIES_PERIOD_RE.match(period)
    if period_match:
        current, maps_in_series = int(period_match.group(1)), int(period_match.group(2))

    if best_of is None:
        best_of = maps_in_series
    if best_of is None or best_of <= 0:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"series length unknown (period={period!r} score={score!r})")

    needed = best_of // 2 + 1
    if wins_a is not None and wins_b is not None:
        if max(wins_a, wins_b) >= needed:
            return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                                  detail=f"series already decided at {wins_a}-{wins_b} (Bo{best_of})")
        # Worst case is that BOTH sides still have to win everything they need bar
        # the one map that ends it: a Bo5 at 1-0 can still run four more maps, even
        # though the leader only needs two. Taking the larger of the two shortfalls
        # would understate that badly, and this estimate must never run short.
        maps_left = (needed - wins_a) + (needed - wins_b) - 1
    elif current is not None:
        # No series score: fall back to map position, assuming the series runs full.
        maps_left = max(1, best_of - current + 1)
    else:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"no series position (period={period!r} score={score!r})")

    # The current map's own progress is not published, so it counts in full.
    remaining = maps_left * map_minutes
    # A series runs at most (2 * needed - 1) maps, which is what "full duration"
    # means for the proportional limit -- a Bo1 of 20-minute maps is a 20-minute
    # contest, not the 90-minute family default.
    full_series = (2 * needed - 1) * map_minutes
    return TimingEstimate(code, remaining, full_series, BASIS_SERIES,
                          detail=f"{maps_left} map(s) left of Bo{best_of} at {map_minutes:.0f} min each")


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^(\d+)", text)
    return int(match.group(1)) if match else None


def _parse_period_number(period: str, rule: SportRule) -> Optional[int]:
    for pattern in (_QUARTER_RE, _HALF_RE, _HOCKEY_PERIOD_RE):
        match = pattern.match(period)
        if match:
            return int(match.group(1) or match.group(2))
    if period.isdigit():
        return int(period)
    return None


# --- Public entry point -----------------------------------------------------------

def estimate_remaining_minutes(event: Any, allow_worst_case: bool = True) -> TimingEstimate:
    """Wall-clock minutes until this live event's match actually finishes.

    Returns a TimingEstimate whose `ok` is False, carrying a machine-readable
    `reason`, whenever the state does not support a reliable answer. `end_date` is
    never consulted.
    """
    code = sport_code(event)

    state = getattr(event, "state", None)
    if state is None or state.live is not True or state.ended is True:
        live = getattr(state, "live", None) if state is not None else None
        ended = getattr(state, "ended", None) if state is not None else None
        return TimingEstimate(code, reason=REASON_NOT_LIVE,
                              detail=f"state.live={live!r} state.ended={ended!r}")

    sports = getattr(event, "sports", None)
    period = _norm_period(getattr(sports, "period", None) if sports else None)
    elapsed = getattr(sports, "elapsed", None) if sports else None
    score = getattr(sports, "score", None) if sports else None

    if period in PERIODS_FINISHED:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"match already finished (period={period!r})")
    if period in PERIODS_NOT_STARTED:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"match not started (period={period!r})")
    if period in PERIODS_NOT_PLAYING:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail=f"play halted, restart time unknown (period={period!r})")

    rule = resolve_rule(event)
    if rule is None:
        return TimingEstimate(code, reason=REASON_SPORT_UNSUPPORTED,
                              detail=f"no timing rule for sport {code!r} (period={period!r}); "
                                     "add one to live_timing.SPORT_RULES to enable it")

    if rule.family == FAMILY_UNSUPPORTED:
        return TimingEstimate(code, reason=REASON_SPORT_UNSUPPORTED,
                              detail=rule.unsupported_detail)

    if not period:
        return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                              detail="no period published for a live event")

    if rule.family == FAMILY_CLOCK_SOCCER:
        return _estimate_soccer(code, period, elapsed)
    if rule.family == FAMILY_PERIOD:
        return _estimate_period_sport(code, rule, period, allow_worst_case)
    if rule.family == FAMILY_SERIES:
        return _estimate_series(code, rule, period, score)

    return TimingEstimate(code, reason=REASON_TIMING_UNAVAILABLE,
                          detail=f"no estimator for family {rule.family!r}")


# --- Eligibility ------------------------------------------------------------------

@dataclass(frozen=True)
class SportSettings:
    """Effective Late Game settings for one sport, after per-sport overrides."""
    enabled: bool = True
    allow_worst_case: bool = True
    max_remaining_minutes: float = 30.0
    max_remaining_fraction: float = 0.34


def sport_settings(code: str, settings: Dict[str, Any]) -> SportSettings:
    """Global Late Game settings overlaid with this sport's own overrides."""
    effective = SportSettings(
        enabled=True,
        allow_worst_case=bool(settings.get("late_game_allow_worst_case_periods", True)),
        max_remaining_minutes=float(settings.get("late_game_max_remaining_minutes", 30.0)),
        max_remaining_fraction=float(settings.get("late_game_max_remaining_fraction", 0.34)),
    )
    overrides = (settings.get("late_game_sport_rules") or {}).get(code)
    if not isinstance(overrides, dict):
        return effective
    return SportSettings(
        enabled=bool(overrides.get("enabled", effective.enabled)),
        allow_worst_case=bool(overrides.get("allow_worst_case", effective.allow_worst_case)),
        max_remaining_minutes=float(overrides.get("max_remaining_minutes",
                                                  effective.max_remaining_minutes)),
        max_remaining_fraction=float(overrides.get("max_remaining_fraction",
                                                   effective.max_remaining_fraction)),
    )


def effective_limit_minutes(estimate: TimingEstimate, sport_cfg: SportSettings) -> float:
    """The tighter of the absolute cap and a proportion of this format's full length.

    A flat minute count means very different things in a 210-minute football game and
    a 20-minute esports map; scaling by the format's own duration keeps "late game"
    meaning the same share of the contest everywhere.
    """
    limit = sport_cfg.max_remaining_minutes
    if sport_cfg.max_remaining_fraction > 0 and estimate.full_match_minutes:
        limit = min(limit, sport_cfg.max_remaining_fraction * estimate.full_match_minutes)
    return limit


@dataclass(frozen=True)
class TimingDecision:
    eligible: bool
    estimate: TimingEstimate
    limit_minutes: Optional[float] = None
    reason: Optional[str] = None
    detail: str = ""

    def describe(self) -> str:
        if self.eligible:
            return (f"late-game OK -- {self.estimate.describe()}, "
                    f"limit {self.limit_minutes:.0f} min")
        return f"late-game rejected [{self.reason}] -- {self.detail}"


REASON_SPORT_DISABLED = "sport_disabled"
REASON_TOO_MUCH_TIME = "too_much_time_remaining"


def evaluate_event_timing(event: Any, settings: Dict[str, Any]) -> TimingDecision:
    """Full timing verdict for one live event, using the configured limits."""
    code = sport_code(event)
    cfg = sport_settings(code, settings)

    if not cfg.enabled:
        estimate = TimingEstimate(code, reason=REASON_SPORT_DISABLED,
                                  detail="disabled by late_game_sport_rules")
        return TimingDecision(False, estimate, None, REASON_SPORT_DISABLED,
                              f"{code} is disabled in late_game_sport_rules")

    estimate = estimate_remaining_minutes(event, allow_worst_case=cfg.allow_worst_case)
    if not estimate.ok:
        return TimingDecision(False, estimate, None, estimate.reason, estimate.describe())

    limit = effective_limit_minutes(estimate, cfg)
    if estimate.remaining_minutes > limit:
        return TimingDecision(
            False, estimate, limit, REASON_TOO_MUCH_TIME,
            f"{estimate.describe()} exceeds the {limit:.0f} min limit",
        )
    return TimingDecision(True, estimate, limit)
