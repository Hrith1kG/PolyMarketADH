"""One entry price band, resolved in one place.

Three controls used to express the same rule -- the Risk tab's Min/Max Price, the
Gates tab's Min/Max Entry Probability, and each account's Price Band -- and they
disagreed. With Late Game on the scanner enforced the Late Game band while the
per-account filter enforced price_min/price_max, so an account silently discarded
signals the scanner had already accepted. On shipped defaults (price_min 0.97, Late
Game band 0.90-0.99) that dropped every Late Game signal with no log line.

These tests pin the resolution so the bands cannot drift apart again.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import settings_manager
from settings_manager import effective_price_band, price_band_keys, with_price_band


def opportunity(price, volume=99_999.0, liquidity=99_999.0, market_type="moneyline"):
    return SimpleNamespace(confirmed_price=price, volume=volume, liquidity=liquidity,
                           market_type=market_type, question="Will Home win?",
                           outcome_label="Yes", token_id="tok")


# --- The resolver -----------------------------------------------------------------

def test_standard_mode_uses_price_min_and_price_max():
    settings = {"price_min": 0.97, "price_max": 0.995,
                "late_game_min_probability": 0.90, "late_game_max_probability": 0.99}
    assert effective_price_band(settings) == (0.97, 0.995)
    assert price_band_keys(settings) == ("price_min", "price_max")


def test_late_game_mode_uses_the_probability_band():
    settings = {"late_game_enabled": True, "price_min": 0.97, "price_max": 0.995,
                "late_game_min_probability": 0.90, "late_game_max_probability": 0.92}
    assert effective_price_band(settings) == (0.90, 0.92)
    assert price_band_keys(settings) == ("late_game_min_probability",
                                         "late_game_max_probability")


def test_widening_writes_back_to_the_keys_in_force():
    late = with_price_band({"late_game_enabled": True}, 0.85, 0.99)
    assert late["late_game_min_probability"] == 0.85
    assert late["late_game_max_probability"] == 0.99
    assert "price_min" not in late

    standard = with_price_band({"late_game_enabled": False}, 0.85, 0.99)
    assert standard["price_min"] == 0.85
    assert standard["price_max"] == 0.99


# --- The per-account filter -------------------------------------------------------

def test_account_without_its_own_band_is_a_no_op(monkeypatch, tmp_path):
    """The regression that silently dropped every Late Game signal.

    Shipped defaults leave price_min at 0.97. An account with no band of its own must
    inherit the band in force (0.90-0.99), not that 0.97 floor.
    """
    settings = dict(settings_manager.DEFAULT_SETTINGS)
    settings["late_game_enabled"] = True
    monkeypatch.setattr(settings_manager, "load_settings", lambda: settings)

    acc = settings_manager.get_account_settings("Anubrata 2")
    assert (acc["price_min"], acc["price_max"]) == (0.90, 0.99)

    for price in (0.91, 0.95, 0.96):
        matches, why_not = main._opportunity_matches_account(opportunity(price), acc)
        assert matches, f"{price} was dropped by the account filter: {why_not}"


def test_account_band_still_narrows_when_explicitly_set(monkeypatch):
    settings = dict(settings_manager.DEFAULT_SETTINGS)
    settings.update({
        "late_game_enabled": True,
        "late_game_min_probability": 0.90,
        "late_game_max_probability": 0.95,
        "account_overrides": {"Anubrata 2": {"price_min": 0.904, "price_max": 0.951}},
    })
    monkeypatch.setattr(settings_manager, "load_settings", lambda: settings)

    acc = settings_manager.get_account_settings("Anubrata 2")
    assert (acc["price_min"], acc["price_max"]) == (0.904, 0.951)

    # Deliberately narrower than the global band: that is the account's choice.
    assert not main._opportunity_matches_account(opportunity(0.900), acc)[0]
    assert main._opportunity_matches_account(opportunity(0.905), acc)[0]


def test_standard_mode_account_inherits_price_min(monkeypatch):
    settings = dict(settings_manager.DEFAULT_SETTINGS)
    settings["late_game_enabled"] = False
    monkeypatch.setattr(settings_manager, "load_settings", lambda: settings)
    acc = settings_manager.get_account_settings("Anubrata 2")
    assert (acc["price_min"], acc["price_max"]) == (0.97, 0.995)


# --- Rejections are explained -----------------------------------------------------

def test_every_account_rejection_carries_a_reason():
    acc = {"price_min": 0.90, "price_max": 0.95, "min_volume": 5000.0,
           "min_liquidity": 1000.0, "sports_market_types": ["moneyline"]}

    cases = [
        (opportunity(0.80), "outside this account's band"),
        (opportunity(0.92, volume=10.0), "volume"),
        (opportunity(0.92, liquidity=10.0), "liquidity"),
        (opportunity(0.92, market_type="totals"), "market type"),
    ]
    for opp, expected in cases:
        matches, why_not = main._opportunity_matches_account(opp, acc)
        assert not matches
        assert expected in why_not, why_not


def test_an_accepted_opportunity_has_no_reason():
    acc = {"price_min": 0.90, "price_max": 0.95, "min_volume": 5000.0,
           "min_liquidity": 1000.0, "sports_market_types": ["moneyline"]}
    assert main._opportunity_matches_account(opportunity(0.92), acc) == (True, "")


# --- The broad scan widens the band actually in force -----------------------------

def test_broad_scan_widens_the_late_game_band_not_price_min(monkeypatch):
    """An account wanting a wider band must get a scan wide enough to serve it."""
    settings = dict(settings_manager.DEFAULT_SETTINGS)
    settings.update({
        "late_game_enabled": True,
        "late_game_min_probability": 0.90,
        "late_game_max_probability": 0.95,
        "account_overrides": {"Wide": {"price_min": 0.85, "price_max": 0.99}},
    })
    monkeypatch.setattr(settings_manager, "load_settings", lambda: settings)

    broad = main._broad_scan_settings(settings, [{"name": "Wide"}])
    assert effective_price_band(broad) == (0.85, 0.99)
    # The standard-mode keys are untouched, so turning Late Game off does not
    # inherit a band that was only ever meant for the live-game scan.
    assert broad["price_min"] == settings_manager.DEFAULT_SETTINGS["price_min"]
