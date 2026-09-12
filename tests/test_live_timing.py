"""Sport-aware late-game timing tests.

The bug this strategy replaced rejected whole sports because their `end_date` meant
something different: soccer and college football publish end_date == start_time,
esports start+6h, tennis and baseball start+7 days. Every test here therefore gives
its event a deliberately unhelpful end_date and asserts the verdict is driven purely
by live in-play state.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import live_timing
from live_timing import evaluate_event_timing, estimate_remaining_minutes


# --- Minimal stand-ins for the SDK models the estimator reads ---------------------

@dataclass
class FakeState:
    live: Optional[bool] = True
    ended: Optional[bool] = False
    closed: Optional[bool] = False


@dataclass
class FakeSport:
    sport: str
    tags: str = ""


@dataclass
class FakeSports:
    sport: FakeSport
    period: Optional[str] = None
    elapsed: Optional[str] = None
    score: Optional[str] = None


@dataclass
class FakeSchedule:
    start_time: Optional[datetime] = None
    end_date: Optional[datetime] = None


@dataclass
class FakeEvent:
    sports: FakeSports
    state: FakeState = field(default_factory=FakeState)
    schedule: Optional[FakeSchedule] = None
    title: str = "Home vs Away"
    tags: Tuple[Any, ...] = ()


NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

# Family tag strings exactly as Gamma publishes them on `sport.tags`.
TAGS_SOCCER = "1,100639,100350,102771"
TAGS_ESPORTS = "1,64,100780,100639"
TAGS_TENNIS = "1,864,100639,101232"
TAGS_CRICKET = "1,100639,517,102803"


def make_event(sport, period=None, elapsed=None, score=None, tags="",
               live=True, ended=False, end_date_offset=timedelta(0)):
    """Builds a live event whose end_date is deliberately misleading.

    `end_date_offset` mimics the real per-sport spread -- 0 for soccer, +7 days for
    tennis -- so any rule that leaked back to end_date would show up immediately.
    """
    return FakeEvent(
        sports=FakeSports(FakeSport(sport, tags), period=period, elapsed=elapsed, score=score),
        state=FakeState(live=live, ended=ended),
        schedule=FakeSchedule(start_time=NOW - timedelta(hours=1),
                              end_date=NOW + end_date_offset),
    )


DEFAULT_SETTINGS = {
    "late_game_max_remaining_minutes": 30.0,
    "late_game_max_remaining_fraction": 0.34,
    "late_game_allow_worst_case_periods": True,
    "late_game_sport_rules": {},
}


def settings(**overrides):
    merged = dict(DEFAULT_SETTINGS)
    merged.update(overrides)
    return merged


# --- Soccer: a real clock, and end_date == start_time ----------------------------

def test_soccer_late_second_half_is_eligible():
    # 80' of a 90' match: ~11 real minutes left. end_date is start_time (already
    # past), exactly the shape that made the old logic accept the whole match.
    event = make_event("kor", period="2H", elapsed="80", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert decision.eligible, decision.describe()
    assert decision.estimate.basis == live_timing.BASIS_CLOCK
    assert decision.estimate.remaining_minutes == pytest.approx(11.5, abs=1.0)


def test_soccer_first_half_is_rejected_as_too_early():
    event = make_event("ukr1", period="1H", elapsed="14", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TOO_MUCH_TIME
    # First half plus half-time plus the whole second half: well over an hour.
    assert decision.estimate.remaining_minutes > 60


def test_soccer_halftime_accounts_for_the_interval():
    event = make_event("kor", period="HT", elapsed="45", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.estimate.remaining_minutes > 60


def test_soccer_suspended_match_is_skipped_not_guessed():
    # Observed live: period='SUS' with an empty elapsed. Play has stopped and the
    # restart time is unknown, which is not the same thing as "nearly over".
    event = make_event("chi1", period="SUS", elapsed="", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TIMING_UNAVAILABLE
    assert "halted" in decision.detail


def test_soccer_without_elapsed_is_skipped():
    event = make_event("kor", period="2H", elapsed=None, tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TIMING_UNAVAILABLE


def test_unknown_soccer_league_still_resolves_via_family_tag():
    # The long tail of soccer leagues is never enumerated by code; the Soccer family
    # tag has to carry them, or a valid opportunity is lost to an unlisted league.
    event = make_event("some-new-league-2027", period="2H", elapsed="85", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert decision.eligible, decision.describe()


# --- Period sports with no in-period clock ---------------------------------------

def test_basketball_fourth_quarter_is_eligible_at_its_own_limit():
    # NBA publishes no clock inside the quarter, so a whole Q4 (~30 wall minutes) is
    # assumed to remain. That clears a 30-minute limit; nothing earlier does.
    event = make_event("nba", period="Q4")
    decision = evaluate_event_timing(event, settings())
    assert decision.eligible, decision.describe()
    assert decision.estimate.basis == live_timing.BASIS_PERIOD
    assert decision.estimate.remaining_minutes == pytest.approx(30.0)


def test_basketball_third_quarter_is_rejected():
    event = make_event("nba", period="Q3")
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TOO_MUCH_TIME


def test_hockey_third_period_needs_its_own_raised_limit():
    # A full NHL third period runs ~40 wall minutes, so it cannot clear a flat 30.
    # This is the case the per-sport override exists for.
    event = make_event("nhl", period="P3")
    assert not evaluate_event_timing(event, settings()).eligible

    raised = settings(late_game_sport_rules={"nhl": {"max_remaining_minutes": 45,
                                                     "max_remaining_fraction": 0}})
    decision = evaluate_event_timing(event, raised)
    assert decision.eligible, decision.describe()
    assert decision.estimate.remaining_minutes == pytest.approx(40.0)


def test_football_fourth_quarter_with_raised_limit():
    # CFB publishes end_date == start_time, the same misleading shape as soccer.
    event = make_event("cfb", period="Q4")
    raised = settings(late_game_sport_rules={"cfb": {"max_remaining_minutes": 60,
                                                     "max_remaining_fraction": 0}})
    decision = evaluate_event_timing(event, raised)
    assert decision.eligible, decision.describe()
    assert decision.estimate.remaining_minutes == pytest.approx(47.0)


def test_nfl_earlier_quarters_include_remaining_intermissions():
    event = make_event("nfl", period="Q2")
    estimate = estimate_remaining_minutes(event)
    # Q2, Q3, Q4 plus two breaks between them.
    assert estimate.remaining_minutes == pytest.approx(3 * 42.0 + 2 * 13.0)


def test_overtime_is_recognised_rather_than_read_as_past_regulation():
    event = make_event("nba", period="OT")
    decision = evaluate_event_timing(event, settings())
    assert decision.eligible, decision.describe()
    assert decision.estimate.remaining_minutes == pytest.approx(12.0)


def test_worst_case_can_be_disabled_per_sport():
    event = make_event("nba", period="Q4")
    disabled = settings(late_game_sport_rules={"nba": {"allow_worst_case": False}})
    decision = evaluate_event_timing(event, disabled)
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TIMING_UNAVAILABLE
    assert "worst-case estimation is disabled" in decision.detail


def test_a_sport_can_be_switched_off_entirely():
    event = make_event("nba", period="Q4")
    off = settings(late_game_sport_rules={"nba": {"enabled": False}})
    decision = evaluate_event_timing(event, off)
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_SPORT_DISABLED


def test_unrecognised_period_is_skipped_and_names_the_value():
    event = make_event("nba", period="Q9")
    decision = evaluate_event_timing(event, settings())
    # Past regulation without matching overtime: treated as an overtime-length tail,
    # never as "unknown means nearly over".
    assert decision.estimate.ok
    event2 = make_event("nba", period="WEIRD")
    decision2 = evaluate_event_timing(event2, settings())
    assert not decision2.eligible
    assert "WEIRD" in decision2.detail


# --- Esports: best-of-N series, end_date == start + 6h ----------------------------

def test_esports_decider_map_is_eligible():
    # Bo3 level at 1-1: the current map decides it, so one 40-minute map remains.
    event = make_event("cs2", period="3/3", score="6-1|1-1|Bo3", tags=TAGS_ESPORTS,
                       end_date_offset=timedelta(hours=6))
    raised = settings(late_game_sport_rules={"cs2": {"max_remaining_minutes": 45,
                                                     "max_remaining_fraction": 0}})
    decision = evaluate_event_timing(event, raised)
    assert decision.eligible, decision.describe()
    assert decision.estimate.basis == live_timing.BASIS_SERIES
    assert decision.estimate.remaining_minutes == pytest.approx(40.0)


def test_esports_series_score_beats_map_number():
    # period says map 3 of 3, but a Bo5 standing at 1-0 can still run four more
    # maps. Reading the map number alone would call this a decider and enter far too
    # early; so would taking only the leader's shortfall.
    event = make_event("lol", period="3/5", score="000-000|1-0|Bo5", tags=TAGS_ESPORTS,
                       end_date_offset=timedelta(hours=6))
    estimate = estimate_remaining_minutes(event)
    assert estimate.remaining_minutes == pytest.approx(4 * 33.0)


def test_esports_early_series_is_rejected():
    event = make_event("dota2", period="1/3", score="0-0|0-0|Bo3", tags=TAGS_ESPORTS,
                       end_date_offset=timedelta(hours=6))
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TOO_MUCH_TIME


def test_short_format_esports_uses_the_proportional_limit():
    # A ~20-minute Honor of Kings map: the proportional cap makes "late game" mean a
    # comparable share of the contest rather than a flat 30 minutes it always passes.
    event = make_event("hok", period="1/1", score="000-000|0-0|Bo1", tags=TAGS_ESPORTS,
                       end_date_offset=timedelta(hours=6))
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TOO_MUCH_TIME
    assert decision.limit_minutes < 30.0


def test_esports_title_without_its_own_duration_uses_the_family_default():
    event = make_event("brandnewtitle", period="2/3", score="0-0|1-1|Bo3",
                       tags=TAGS_ESPORTS, end_date_offset=timedelta(hours=6))
    estimate = estimate_remaining_minutes(event)
    assert estimate.remaining_minutes == pytest.approx(live_timing.ESPORTS_DEFAULT_MAP_MINUTES)


def test_esports_already_decided_series_is_skipped():
    event = make_event("cs2", period="3/3", score="6-1|2-0|Bo3", tags=TAGS_ESPORTS)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_TIMING_UNAVAILABLE


# --- Formats with no usable timing ------------------------------------------------

def test_tennis_is_skipped_with_a_reason_not_rejected_for_its_end_date():
    # Tennis end_date is start + 7 days. It is skipped because a set has no bounded
    # duration -- not because of how its dates look.
    event = make_event("atp", period="S2", score="3-6, 4-5", tags=TAGS_TENNIS,
                       end_date_offset=timedelta(days=7))
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_SPORT_UNSUPPORTED
    assert "bounded" in decision.detail


def test_cricket_is_skipped_with_a_reason():
    event = make_event("crint", period="Live", score="453-185", tags=TAGS_CRICKET,
                       end_date_offset=timedelta(days=7))
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_SPORT_UNSUPPORTED


def test_baseball_is_skipped_until_an_inning_field_exists():
    event = make_event("mlb", period="VFT", end_date_offset=timedelta(days=7))
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible


def test_unknown_sport_is_skipped_and_names_itself():
    event = make_event("kabaddi", period="2H", elapsed="35")
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_SPORT_UNSUPPORTED
    assert "kabaddi" in decision.detail


# --- Liveness ---------------------------------------------------------------------

def test_event_not_live_is_rejected_before_any_timing_work():
    event = make_event("kor", period="2H", elapsed="85", tags=TAGS_SOCCER, live=False)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_NOT_LIVE


def test_ended_event_is_rejected_even_when_flagged_live():
    event = make_event("kor", period="2H", elapsed="90", tags=TAGS_SOCCER, ended=True)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_NOT_LIVE


def test_finished_period_is_not_read_as_nearly_over():
    event = make_event("kor", period="FT", elapsed="90", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings())
    assert not decision.eligible
    assert "finished" in decision.detail


# --- Limit arithmetic -------------------------------------------------------------

def test_absolute_and_proportional_caps_take_the_tighter_one():
    event = make_event("kor", period="2H", elapsed="70", tags=TAGS_SOCCER)
    # Soccer's full duration is ~115 min, so fraction 0.1 gives ~11.5 min -- tighter
    # than the 30-minute absolute cap, and 20' left no longer qualifies.
    tight = settings(late_game_max_remaining_fraction=0.1)
    assert not evaluate_event_timing(event, tight).eligible
    loose = settings(late_game_max_remaining_fraction=0)
    assert evaluate_event_timing(event, loose).eligible


def test_fraction_of_zero_disables_the_proportional_cap():
    event = make_event("hok", period="1/1", score="000-000|0-0|Bo1", tags=TAGS_ESPORTS)
    decision = evaluate_event_timing(event, settings(late_game_max_remaining_fraction=0))
    assert decision.eligible, decision.describe()
    assert decision.limit_minutes == pytest.approx(30.0)


# --- Clock-only policy ------------------------------------------------------------
#
# The strategy trades where remaining time is actually known and skips where it is
# not, rather than acting on a bound that is technically correct but far too wide.

def test_shipped_defaults_skip_sports_with_no_in_period_clock():
    import settings_manager
    shipped = dict(settings_manager.DEFAULT_SETTINGS)
    for sport, period in (("nba", "Q4"), ("nhl", "P3"), ("nfl", "Q4"), ("cfb", "Q4")):
        decision = evaluate_event_timing(make_event(sport, period=period), shipped)
        assert not decision.eligible, f"{sport} should be skipped by default"
        assert decision.reason == live_timing.REASON_TIMING_UNAVAILABLE
        assert "worst-case estimation is disabled" in decision.detail


def test_shipped_defaults_still_trade_a_sport_with_a_real_clock():
    import settings_manager
    shipped = dict(settings_manager.DEFAULT_SETTINGS)
    event = make_event("kor", period="2H", elapsed="82", tags=TAGS_SOCCER)
    assert evaluate_event_timing(event, shipped).eligible


def test_worst_case_can_be_switched_back_on_per_sport():
    # Opting one sport back in stays possible without touching the global switch.
    event = make_event("nba", period="Q4")
    opted_in = settings(late_game_allow_worst_case_periods=False,
                        late_game_sport_rules={"nba": {"allow_worst_case": True}})
    assert evaluate_event_timing(event, opted_in).eligible


def test_require_clock_skips_esports_which_is_counted_not_clocked():
    event = make_event("cs2", period="3/3", score="6-1|1-1|Bo3", tags=TAGS_ESPORTS,
                       end_date_offset=timedelta(hours=6))
    permissive = settings(late_game_max_remaining_minutes=60,
                          late_game_max_remaining_fraction=0)
    assert evaluate_event_timing(event, permissive).eligible

    strict = settings(late_game_max_remaining_minutes=60,
                      late_game_max_remaining_fraction=0,
                      late_game_require_clock=True)
    decision = evaluate_event_timing(event, strict)
    assert not decision.eligible
    assert decision.reason == live_timing.REASON_NO_CLOCK
    assert "no in-play game clock" in decision.detail


def test_require_clock_still_allows_soccer():
    event = make_event("kor", period="2H", elapsed="82", tags=TAGS_SOCCER)
    decision = evaluate_event_timing(event, settings(late_game_require_clock=True))
    assert decision.eligible
    assert decision.estimate.basis == live_timing.BASIS_CLOCK


def test_require_clock_can_be_relaxed_for_one_sport():
    event = make_event("cs2", period="3/3", score="6-1|1-1|Bo3", tags=TAGS_ESPORTS)
    relaxed = settings(late_game_require_clock=True,
                       late_game_max_remaining_minutes=60,
                       late_game_sport_rules={"cs2": {"require_clock": False,
                                                      "max_remaining_fraction": 0}})
    assert evaluate_event_timing(event, relaxed).eligible
