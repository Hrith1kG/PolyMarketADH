"""Guards that adding the Crypto 5-Minute strategy changed nothing about Sports.

Crypto and Sports are separate strategies with separate settings, separate
candidate discovery and separate timing rules. Sports behaviour itself is
covered by test_live_timing.py and test_scanner_late_game.py; this file asserts
the *separation*: the sports defaults, the sports timing decision, the sports
scan query and the sports scan cadence must be exactly what they would be if
the crypto strategy did not exist.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"] = "state_su.json"
os.environ["TRADES_DB_FILE"] = "trades_su.db"
os.environ["SETTINGS_FILE"] = "settings_su.json"

import live_timing
import main
import scanner
import settings_manager
from tests.crypto_fixtures import FakeClient
from tests.test_live_timing import FakeEvent, FakeSports, FakeSport, TAGS_SOCCER

# 1. Every sports default is untouched, and crypto contributed only namespaced
#    keys of its own.
EXPECTED_SPORTS_DEFAULTS = {
    "price_min": 0.97,
    "price_max": 0.995,
    "min_volume": 5000.0,
    "min_liquidity": 1000.0,
    "min_hours_to_resolution": 1.0,
    "max_days_to_resolution": 30.0,
    "stake_per_trade": 25.0,
    "max_open_positions": 10,
    "max_total_exposure": 200.0,
    "max_trades_per_day": 10,
    "max_signals_per_scan": 5,
    "poll_interval_seconds": 60,
    "only_sports": True,
    "sports_market_types": ["moneyline"],
    "sports_tag_id": 100639,
    "live_trading": False,
    "entry_kill_switch": False,
    "max_slippage": 0.005,
    "account_stakes": {},
    "account_overrides": {},
    "bot_status": "RUNNING",
    "manual_scan_requested": False,
    "tracked_wallet_address": "",
    "require_healthy_data": True,
    "require_high_confidence": False,
    "late_game_enabled": False,
    "late_game_max_remaining_minutes": 30.0,
    "late_game_max_remaining_fraction": 0.34,
    "late_game_min_probability": 0.90,
    "late_game_max_probability": 0.99,
    "late_game_allow_worst_case_periods": False,
    "late_game_require_clock": False,
    "late_game_sport_rules": {},
}
for key, expected in EXPECTED_SPORTS_DEFAULTS.items():
    actual = settings_manager.DEFAULT_SETTINGS[key]
    assert actual == expected, f"sports default {key} changed: {actual!r} != {expected!r}"

extra = set(settings_manager.DEFAULT_SETTINGS) - set(EXPECTED_SPORTS_DEFAULTS)
assert all(k.startswith("crypto_") for k in extra), f"non-namespaced new settings: {extra}"
missing = set(EXPECTED_SPORTS_DEFAULTS) - set(settings_manager.DEFAULT_SETTINGS)
assert not missing, f"sports settings removed: {missing}"
print("PASS every sports default is unchanged and all new settings are crypto_-namespaced")

# 2. The sports modules know nothing about crypto, and the crypto config reader
#    touches no sports setting. Rather than grep for key names, the reader is
#    watched while it runs.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for module in ("scanner.py", "live_timing.py"):
    source = open(os.path.join(REPO, module)).read().lower()
    assert "crypto" not in source, f"{module} must not reference the crypto strategy"

import crypto_scanner


class RecordingSettings(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_keys = []

    def get(self, key, default=None):
        self.read_keys.append(key)
        return super().get(key, default)


watched = RecordingSettings(settings_manager.DEFAULT_SETTINGS)
crypto_scanner.crypto_settings(watched)
non_crypto = [k for k in watched.read_keys if not k.startswith("crypto_")]
assert not non_crypto, f"the crypto config reader touched sports settings: {non_crypto}"
print("PASS neither strategy reads the other's settings or code")

# 3. Sports timing is still decided from live in-play state, not from a fixed
#    round-end timestamp. The crypto strategy's use of end_date must not have
#    leaked into the sports path.
def soccer(period, elapsed):
    return FakeEvent(sports=FakeSports(period=period, elapsed=elapsed,
                                       sport=FakeSport(sport="kor", tags=TAGS_SOCCER)))

settings = dict(settings_manager.DEFAULT_SETTINGS)
late = live_timing.evaluate_event_timing(soccer("2H", "85"), settings)
early = live_timing.evaluate_event_timing(soccer("1H", "10"), settings)
assert late.eligible is True, late.describe()
assert early.eligible is False, early.describe()
assert late.estimate.basis == live_timing.BASIS_CLOCK, late.estimate.basis
# The same event with no live in-play state is refused, whatever any timestamp says.
blind = live_timing.evaluate_event_timing(soccer("", None), settings)
assert blind.eligible is False, blind.describe()
print("PASS sports timing is still driven by live in-play state, not a fixed timestamp")

# 4. The sports scan still queries Gamma the sports way -- live events under the
#    sports tag -- with no end-date bound, which is a crypto-only device.
class SportsProbe(FakeClient):
    def __init__(self):
        super().__init__()
        self.event_calls = []

    def list_events(self, **kwargs):
        self.event_calls.append(kwargs)
        return []

    def list_markets(self, **kwargs):
        self.list_calls.append(kwargs)
        return []


import polymarket_client

probe = SportsProbe()
polymarket_client.get_public_client = lambda: probe

late_game_settings = dict(settings_manager.DEFAULT_SETTINGS)
late_game_settings["late_game_enabled"] = True
scanner.find_opportunities(settings_override=late_game_settings)
assert probe.event_calls, "the Late Game scan must still discover through live events"
event_query = probe.event_calls[0]
assert event_query["live"] is True and event_query["closed"] is False, event_query
assert event_query["tag_ids"] == 100639, event_query
assert "end_date_min" not in event_query and "end_date_max" not in event_query, event_query

scanner.find_opportunities(settings_override=dict(settings_manager.DEFAULT_SETTINGS))
market_query = probe.list_calls[0]
assert market_query["tag_id"] == 100639, market_query
assert market_query["sports_market_types"] == ["moneyline"], market_query
assert "end_date_min" not in market_query and "end_date_max" not in market_query, market_query
print("PASS both sports scan paths query exactly as before, with no end-date bound")

# 5. The two polling cadences are separate settings and move independently.
loaded = settings_manager.load_settings()
assert loaded["poll_interval_seconds"] == 60
assert loaded["crypto_poll_interval_seconds"] == 3
settings_manager.update_setting("crypto_poll_interval_seconds", 2)
assert settings_manager.load_settings()["poll_interval_seconds"] == 60, \
    "changing the crypto cadence must not move the sports cadence"
settings_manager.update_setting("poll_interval_seconds", 120)
assert settings_manager.load_settings()["crypto_poll_interval_seconds"] == 2, \
    "changing the sports cadence must not move the crypto cadence"
print("PASS the sports and crypto polling cadences are independent settings")

# 6. The sports execution path still filters on sports market types and would
#    never admit a crypto opportunity.
class Opp:
    def __init__(self, market_type):
        self.confirmed_price = 0.98
        self.volume = 10000.0
        self.liquidity = 5000.0
        self.market_type = market_type


acc_settings = {"price_min": 0.97, "price_max": 0.995, "min_volume": 5000.0,
                "min_liquidity": 1000.0, "sports_market_types": ["moneyline"]}
ok, _why = main._opportunity_matches_account(Opp("moneyline"), acc_settings)
assert ok is True, _why
ok, why = main._opportunity_matches_account(Opp("crypto_5m_up_down"), acc_settings)
assert ok is False and "crypto_5m_up_down" in why, why
print("PASS the sports execution path still admits moneylines only")

# 7. A crypto position does not consume the sports daily budget, and a sports
#    position does not consume the crypto one.
import paper_broker

broker = paper_broker.PaperBroker()


class Pos:
    def __init__(self, token, market_type):
        self.token_id = token
        self.market_id = "m" + token
        self.question = "q"
        self.outcome_label = "Up"
        self.confirmed_price = 0.95
        self.end_date = None
        self.slug = "slug"
        self.market_type = market_type
        self.game_start_time = None


broker.open_position(Pos("s1", "moneyline"), stake=10.0, mode="PAPER")
assert broker.crypto_trades_today() == 0, "a sports entry must not count against the crypto cap"
assert broker.crypto_positions() == {}, "a sports entry must not appear in the crypto book"
broker.open_position(Pos("c1", "crypto_5m_up_down"), stake=10.0, mode="PAPER")
broker.record_crypto_trade()
assert broker.crypto_trades_today() == 1
assert set(broker.crypto_positions()) == {"c1"}, broker.crypto_positions()
print("PASS the two strategies keep separate books and separate daily budgets")

# 8. Crypto signals live under their own state key, so neither feed erases the
#    other on its own cadence.
class Signal:
    def __init__(self, name):
        self.name = name

    def to_dict(self):
        return {"question": self.name}


broker.save_signals([Signal("sports")])
broker.save_crypto_signals([Signal("crypto")])
state = broker.reload()
assert state["signals"] == [{"question": "sports"}], state["signals"]
assert state["crypto_signals"] == [{"question": "crypto"}], state["crypto_signals"]
broker.save_signals([Signal("sports again")])
assert broker.reload()["crypto_signals"] == [{"question": "crypto"}], "a sports scan erased the crypto feed"
print("PASS the two signal feeds are stored separately and do not overwrite each other")

print("\nALL sports-unchanged TESTS PASSED")
