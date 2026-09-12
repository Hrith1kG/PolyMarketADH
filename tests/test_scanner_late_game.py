"""End-to-end Late Game scan tests against a stubbed Polymarket client.

Covers the four eligibility conditions together: authoritatively live, sport-aware
timing inside the limit, the exact token priced inside the configured band, and the
existing liquidity / spread / duplicate-position / quote-freshness checks.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import scanner
import polymarket_client
from tests.test_live_timing import (
    FakeEvent, FakeSports, FakeSport, FakeState, FakeSchedule,
    TAGS_SOCCER, TAGS_ESPORTS, TAGS_TENNIS,
)

NOW = datetime.now(timezone.utc)


# --- Stub SDK surface -------------------------------------------------------------

@dataclass
class FakeOutcome:
    label: str
    token_id: str
    price: Optional[float]


@dataclass
class FakeOutcomes:
    yes: Optional[FakeOutcome] = None
    no: Optional[FakeOutcome] = None


@dataclass
class FakeMetrics:
    volume: float = 50_000.0
    liquidity: float = 20_000.0


@dataclass
class FakeMarketState:
    closed: bool = False
    accepting_orders: bool = True
    # Deliberately misleading: this is the field the old logic trusted.
    end_date: Optional[datetime] = None


@dataclass
class FakeMarketSports:
    sports_market_type: str = "moneyline"
    game_start_time: Optional[datetime] = None


@dataclass
class FakeMarket:
    id: str
    question: str
    slug: str = ""
    outcomes: Optional[FakeOutcomes] = None
    metrics: FakeMetrics = field(default_factory=FakeMetrics)
    state: FakeMarketState = field(default_factory=FakeMarketState)
    sports: FakeMarketSports = field(default_factory=FakeMarketSports)


@dataclass
class FakeEventWithMarkets(FakeEvent):
    id: str = "ev1"
    slug: str = ""
    markets: tuple = ()


@dataclass
class FakeLevel:
    price: float
    size: float = 10_000.0


@dataclass
class FakeBook:
    bids: List[FakeLevel]
    asks: List[FakeLevel]
    timestamp: Optional[datetime] = None


@dataclass
class FakePage:
    items: list


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def __iter__(self):
        return iter(self._pages)


class FakeClient:
    """Serves one page of live events plus a healthy book for every token."""

    def __init__(self, events, books=None):
        self.events = events
        self.books = books or {}
        self.list_events_kwargs = None
        self.list_markets_called = False

    def list_events(self, **kwargs):
        self.list_events_kwargs = kwargs
        return FakePaginator([FakePage(self.events)])

    def list_markets(self, **kwargs):
        self.list_markets_called = True
        return FakePaginator([FakePage([])])

    def get_order_book(self, token_id):
        if token_id in self.books:
            return self.books[token_id]
        return FakeBook(bids=[FakeLevel(0.90)], asks=[FakeLevel(0.91)], timestamp=NOW)

    def get_price(self, token_id, side):
        return Decimal("0.91")


def soccer_event(period="2H", elapsed="85", price=0.91, token="tok-a",
                 market_type="moneyline", volume=50_000.0, liquidity=20_000.0,
                 live=True, sport="kor", tags=TAGS_SOCCER, end_date=None):
    """A live soccer event whose end_date is its start_time, already in the past.

    That is the real shape of the payload, and the shape the previous end_date rule
    could not read correctly.
    """
    market = FakeMarket(
        id="m1",
        question="Will Home win?",
        slug="home-win",
        outcomes=FakeOutcomes(
            yes=FakeOutcome("Yes", token, price),
            no=FakeOutcome("No", "tok-b", None if price is None else round(1 - price, 3)),
        ),
        metrics=FakeMetrics(volume=volume, liquidity=liquidity),
        state=FakeMarketState(end_date=end_date or (NOW - timedelta(hours=1))),
        sports=FakeMarketSports(sports_market_type=market_type,
                                game_start_time=NOW - timedelta(hours=1)),
    )
    return FakeEventWithMarkets(
        sports=FakeSports(FakeSport(sport, tags), period=period, elapsed=elapsed, score="2-0"),
        state=FakeState(live=live, ended=False),
        schedule=FakeSchedule(start_time=NOW - timedelta(hours=1)),
        title="Home vs Away",
        markets=(market,),
    )


BASE_SETTINGS = {
    "late_game_enabled": True,
    "late_game_max_remaining_minutes": 30.0,
    "late_game_max_remaining_fraction": 0.34,
    "late_game_min_probability": 0.90,
    "late_game_max_probability": 0.92,
    "late_game_allow_worst_case_periods": True,
    "late_game_sport_rules": {},
    "min_volume": 5000.0,
    "min_liquidity": 1000.0,
    "only_sports": True,
    "sports_market_types": ["moneyline"],
    "sports_tag_id": 100639,
    "require_healthy_data": True,
    "require_high_confidence": False,
    "stake_per_trade": 25.0,
    "max_signals_per_scan": 5,
}


def run_scan(monkeypatch, client, held=None, **setting_overrides):
    settings = dict(BASE_SETTINGS)
    settings.update(setting_overrides)
    monkeypatch.setattr(polymarket_client, "get_public_client", lambda: client)
    return scanner.find_opportunities(held_token_ids=held or set(),
                                      settings_override=settings)


def reasons():
    return {row["reason"] for row in scanner.LAST_SCAN_REJECTIONS}


# --- The happy path ---------------------------------------------------------------

def test_late_soccer_in_band_produces_a_signal(monkeypatch):
    client = FakeClient([soccer_event()])
    results = run_scan(monkeypatch, client)
    assert len(results) == 1
    opp = results[0]
    assert opp.token_id == "tok-a"
    assert opp.sport == "kor"
    assert opp.timing_basis == "clock"
    assert opp.remaining_minutes < 30
    assert scanner.LAST_SCAN_ERROR is None


def test_scan_uses_the_server_side_live_filter(monkeypatch):
    client = FakeClient([soccer_event()])
    run_scan(monkeypatch, client)
    assert client.list_events_kwargs["live"] is True
    assert client.list_events_kwargs["closed"] is False
    assert client.list_markets_called is False


def test_late_game_off_uses_the_market_scan(monkeypatch):
    client = FakeClient([soccer_event()])
    run_scan(monkeypatch, client, late_game_enabled=False)
    assert client.list_markets_called is True


# --- Each rejection reason --------------------------------------------------------

def test_event_not_live_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(live=False)])
    assert run_scan(monkeypatch, client) == []
    assert "not_live" in reasons()


def test_too_much_time_remaining_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(period="1H", elapsed="10")])
    assert run_scan(monkeypatch, client) == []
    assert "too_much_time_remaining" in reasons()


def test_timing_unavailable_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(period="SUS", elapsed="")])
    assert run_scan(monkeypatch, client) == []
    assert "timing_unavailable" in reasons()


def test_unsupported_sport_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(sport="atp", tags=TAGS_TENNIS, period="S2")])
    assert run_scan(monkeypatch, client) == []
    assert "sport_unsupported" in reasons()


def test_probability_below_the_band_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(price=0.85)])
    assert run_scan(monkeypatch, client) == []
    assert "probability_out_of_band" in reasons()


def test_probability_above_the_band_is_rejected(monkeypatch):
    # The band is a band: 0.98 is outside a 0.90-0.92 configuration.
    client = FakeClient([soccer_event(price=0.98)])
    assert run_scan(monkeypatch, client) == []
    assert "probability_out_of_band" in reasons()


def test_thin_liquidity_is_rejected(monkeypatch):
    client = FakeClient([soccer_event(liquidity=10.0)])
    assert run_scan(monkeypatch, client) == []
    assert "liquidity_or_volume" in reasons()


def test_already_held_token_is_rejected(monkeypatch):
    client = FakeClient([soccer_event()])
    assert run_scan(monkeypatch, client, held={"tok-a"}) == []
    assert "already_held" in reasons()


def test_wide_spread_is_rejected(monkeypatch):
    client = FakeClient([soccer_event()],
                        books={"tok-a": FakeBook(bids=[FakeLevel(0.50)],
                                                 asks=[FakeLevel(0.91)], timestamp=NOW)})
    assert run_scan(monkeypatch, client) == []
    assert "unhealthy_book" in reasons()


def test_stale_quote_is_rejected_without_affecting_game_timing(monkeypatch):
    # Quote freshness and game timing are separate concerns: the match is still late,
    # but the market data is too old to act on, so this fails the book check only.
    stale = NOW - timedelta(minutes=30)
    client = FakeClient([soccer_event()],
                        books={"tok-a": FakeBook(bids=[FakeLevel(0.90)],
                                                 asks=[FakeLevel(0.91)], timestamp=stale)})
    assert run_scan(monkeypatch, client) == []
    # The favourite fails on the book alone -- no timing reason appears, because a
    # stale quote says nothing about how far the match has progressed.
    favourite = [r for r in scanner.LAST_SCAN_REJECTIONS if "tok-a" not in r["subject"]
                 and r["subject"].endswith("[Yes]")]
    assert [r["reason"] for r in favourite] == ["unhealthy_book"]
    assert not reasons() & {"not_live", "timing_unavailable", "too_much_time_remaining"}


def test_non_moneyline_market_is_filtered_out(monkeypatch):
    client = FakeClient([soccer_event(market_type="soccer_exact_score")])
    assert run_scan(monkeypatch, client) == []
    assert "market_filtered" in reasons()


# --- Sports must not be excluded by how they represent dates ----------------------

@pytest.mark.parametrize("sport,tags,period,elapsed,score,end_offset,rules", [
    # Soccer: end_date == start_time, already in the past.
    ("kor", TAGS_SOCCER, "2H", "85", "2-0", timedelta(0), {}),
    # College football: same misleading end_date, quarter-based timing.
    ("cfb", "1,100351,100639", "Q4", None, "21-28", timedelta(0),
     {"cfb": {"max_remaining_minutes": 60, "max_remaining_fraction": 0}}),
    # Basketball: no in-period clock at all.
    ("nba", "1,745,100639", "Q4", None, "98-91", timedelta(0), {}),
    # Hockey: needs its own raised limit for a full third period.
    ("nhl", "1,899,100639", "P3", None, "3-1", timedelta(0),
     {"nhl": {"max_remaining_minutes": 45, "max_remaining_fraction": 0}}),
    # Esports: end_date is start + 6 hours, timing comes from the series score.
    ("cs2", TAGS_ESPORTS, "3/3", None, "6-1|1-1|Bo3", timedelta(hours=6),
     {"cs2": {"max_remaining_minutes": 45, "max_remaining_fraction": 0}}),
])
def test_each_sport_can_produce_a_signal(monkeypatch, sport, tags, period, elapsed,
                                         score, end_offset, rules):
    """A valid late-game opportunity must not be lost because that sport writes its
    event dates differently from the others."""
    event = soccer_event(sport=sport, tags=tags, period=period, elapsed=elapsed,
                         end_date=NOW + end_offset)
    event.sports.score = score
    client = FakeClient([event])
    results = run_scan(monkeypatch, client, late_game_sport_rules=rules)
    assert len(results) == 1, f"{sport} rejected: {scanner.LAST_SCAN_REJECTIONS}"
    assert results[0].sport == sport


def test_rejection_summary_counts_reasons(monkeypatch):
    client = FakeClient([soccer_event(price=0.85), soccer_event(live=False)])
    run_scan(monkeypatch, client)
    summary = scanner.rejection_summary()
    assert summary.get("not_live") == 1
    assert summary.get("probability_out_of_band", 0) >= 1
