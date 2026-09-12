"""Central settings manager for the Polymarket Sureshot Bot.
Allows the Streamlit control panel to update thresholds dynamically at runtime."""
import json
import os
from typing import Any, Dict

SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Price threshold
    "price_min": 0.97,
    "price_max": 0.995,
    # Liquidity & Volume
    "min_volume": 5000.0,
    "min_liquidity": 1000.0,
    # Resolution window
    "min_hours_to_resolution": 1.0,
    "max_days_to_resolution": 30.0,
    # Risk & Sizing
    "stake_per_trade": 25.0,
    "max_open_positions": 10,
    "max_total_exposure": 200.0,
    "max_trades_per_day": 10,
    "max_signals_per_scan": 5,
    "poll_interval_seconds": 60,
    # Category & Market Filters
    "only_sports": True,
    "sports_market_types": ["moneyline"],
    "sports_tag_id": 100639,
    # Execution mode & Bot status
    "live_trading": False,
    "entry_kill_switch": False,  # Blocks new trade entries when True
    "max_slippage": 0.005,       # 0.5% max price slippage
    "account_stakes": {},        # Dynamic per-account stake overrides: {"AccountName": 25.0}
    "account_overrides": {},     # Per-account independent controls: {"AccountName": {key: value}}
    "bot_status": "RUNNING",     # "RUNNING" or "PAUSED"
    "manual_scan_requested": False,
    # Wallet Tracking (Data API)
    "tracked_wallet_address": "",
    # Health & Confidence Gates
    "require_healthy_data": True,
    "require_high_confidence": False,
    # Late Game Settings
    #
    # "Late game" is decided from authoritative live in-play state (see live_timing.py
    # and docs/LATE_GAME_TIMING.md), never from end_date -- end_date means a different
    # thing in every sport and says nothing about when a match finishes.
    "late_game_enabled": False,
    # Absolute cap on estimated wall-clock minutes left in the match.
    "late_game_max_remaining_minutes": 30.0,
    # Proportional cap: a share of that format's own typical full duration, so the
    # rule means the same thing in a 210-minute football game and a 20-minute esports
    # map. The tighter of the two caps applies. Set to 0 to use the absolute cap only.
    "late_game_max_remaining_fraction": 0.34,
    # Entry probability band for the exact outcome token being bought. While Late Game
    # is enabled these replace price_min/price_max so the configured band is the band.
    "late_game_min_probability": 0.90,
    "late_game_max_probability": 0.99,
    # Sports with no in-period clock (NBA quarters, NHL periods) publish nothing that
    # says how far into the period play has got. Off by default: trade where the
    # remaining time is actually known, skip where it is not. Turning this on falls
    # back to assuming the whole current period remains -- correct, but a bound wide
    # enough that those sports only qualify once the limit is raised to match.
    "late_game_allow_worst_case_periods": False,
    # Strictest form of the same rule: accept only sports that publish a real in-play
    # clock (soccer). Esports, timed by counting remaining maps rather than reading a
    # clock, is skipped as well.
    "late_game_require_clock": False,
    # Per-sport overrides keyed by sport code, e.g.
    #   {"nfl": {"max_remaining_minutes": 45}, "nhl": {"enabled": False}}
    # Recognised keys: enabled, allow_worst_case, max_remaining_minutes,
    # max_remaining_fraction.
    "late_game_sport_rules": {},

    # =====================================================================
    # Crypto 5-Minute strategy -- a SEPARATE strategy from Sports.
    # Every key is namespaced `crypto_*` and is read only by crypto_scanner /
    # crypto_strategy. No sports key is consulted by the crypto path and no
    # crypto key is consulted by scanner.py, so the two can be tuned (and
    # paused, and kill-switched) completely independently.
    # =====================================================================
    "crypto_enabled": False,           # off until deliberately switched on
    "crypto_bot_status": "RUNNING",    # "RUNNING" or "PAUSED", crypto only
    "crypto_entry_kill_switch": False, # blocks crypto entries only
    # Approved universe. Anything outside crypto_markets.APPROVED_ASSETS is
    # ignored even if it is listed here.
    "crypto_assets": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
    # Entry timing: trade only when 0 < seconds_remaining <= this value,
    # measured against the round's authoritative end timestamp.
    "crypto_entry_window_seconds": 30,
    # Probability floor: the executable ask on the side being bought.
    "crypto_min_probability": 0.90,
    # Ceiling: at ~1.00 there is no profit left to pay for the tail risk, and
    # the fill is pure downside. Raise to 1.0 to disable.
    "crypto_max_probability": 0.999,
    # Polling must be fast enough not to miss a 30-second window. This is the
    # crypto cadence only -- poll_interval_seconds still governs sports.
    "crypto_poll_interval_seconds": 3,
    # Risk budget, kept separate from the sports budget.
    "crypto_stake_per_trade": 25.0,
    "crypto_max_open_positions": 5,
    "crypto_max_total_exposure": 100.0,
    "crypto_max_trades_per_day": 20,
    "crypto_max_slippage": 0.01,
    # Gamma volume/liquidity floors. Leave crypto_min_volume at 0: the live API
    # returns volume=null on a round only minutes old, so any floor above 0
    # rejects every genuine round. Liquidity IS populated (a few hundred to a
    # few thousand dollars is typical). The order-book gates below carry the
    # real liquidity requirement either way.
    "crypto_min_volume": 0.0,
    "crypto_min_liquidity": 0.0,
    # Order-book health for the side being bought. Observed live: these books
    # quote a 0.01 spread on a 0.01 tick, with anywhere from ~18 to ~1400 shares
    # resting at the best ask -- so the depth multiple below is the gate that
    # bites most often. See README.md before raising the stake.
    "crypto_max_spread": 0.05,
    "crypto_max_quote_age_seconds": 20.0,
    "crypto_min_ask_depth_multiple": 1.0,
    # Require a resting bid as well as an offer before trusting the quote. Near
    # the end of a round the favourite's book routinely goes offer-only, so this
    # is the difference between "conservative" and "never trades in the last 30
    # seconds". An offer with no bid is still executable; the depth and quote-age
    # gates above still apply to it either way. Leave True unless the logs show
    # book_no_bids costing you every entry.
    "crypto_require_two_sided_book": True,
    # Round shape. These markets are fixed 5-minute rounds; a market whose
    # measured duration is outside this band is not one of them and is skipped.
    "crypto_round_duration_seconds": 300,
    "crypto_round_duration_tolerance_seconds": 20,
    # Discovery: how far past the entry window to look for upcoming rounds, so
    # a round is already known when its window opens.
    "crypto_discovery_lookahead_seconds": 420,
    # Optional Gamma tag id to narrow the scan. Leave null: these rounds are
    # served with an empty tags array, so a tag filter finds nothing. Discovery
    # is bounded by resolution time instead, which needs no tag.
    "crypto_discovery_tag_id": None,
    "crypto_discovery_page_size": 100,
    "crypto_discovery_max_pages": 3,
    "crypto_order_type": "LIMIT",      # "LIMIT" or "MARKET"
}

# Late Game settings that were replaced by the live-state rewrite. They are dropped on
# load so a settings.json written by the old end_date logic cannot silently reintroduce
# a threshold nothing reads any more.
RETIRED_SETTINGS = ("require_authoritative_time", "late_game_threshold_seconds")



# --- The entry price band -----------------------------------------------------
#
# There is one price band, and which keys hold it depends on the mode. Late Game
# reads late_game_min/max_probability; the standard scan reads price_min/price_max.
# Everything that filters on entry price MUST resolve it through these helpers --
# reading price_min directly is how the per-account filter ended up silently
# discarding Late Game signals that the scanner had already accepted.

def effective_price_band(settings: Dict[str, Any]) -> tuple:
    """The (min, max) entry price band actually in force for these settings."""
    if settings.get("late_game_enabled", False):
        return (float(settings.get("late_game_min_probability", 0.90)),
                float(settings.get("late_game_max_probability", 0.99)))
    return (float(settings.get("price_min", 0.97)),
            float(settings.get("price_max", 0.995)))


def price_band_keys(settings: Dict[str, Any]) -> tuple:
    """The two setting keys holding the band in force, for writing it back."""
    if settings.get("late_game_enabled", False):
        return ("late_game_min_probability", "late_game_max_probability")
    return ("price_min", "price_max")


def with_price_band(settings: Dict[str, Any], low: float, high: float) -> Dict[str, Any]:
    """Copy of `settings` with the in-force band replaced by (low, high)."""
    low_key, high_key = price_band_keys(settings)
    updated = dict(settings)
    updated[low_key] = float(low)
    updated[high_key] = float(high)
    return updated


def load_settings() -> Dict[str, Any]:
    """Loads current settings from JSON, filling in any missing defaults."""
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
                    for retired in RETIRED_SETTINGS:
                        settings.pop(retired, None)
        except Exception as e:
            print(f"[settings_manager] Warning: Failed to read {SETTINGS_FILE}: {e}")
    else:
        save_settings(settings)
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    """Saves updated settings to JSON."""
    try:
        tmp_file = f"{SETTINGS_FILE}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp_file, SETTINGS_FILE)
    except Exception as e:
        print(f"[settings_manager] Error saving {SETTINGS_FILE}: {e}")


def update_setting(key: str, value: Any) -> None:
    """Updates a single setting key and persists immediately."""
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def get_setting(key: str, default: Any = None) -> Any:
    """Gets a single setting value."""
    settings = load_settings()
    return settings.get(key, default)


def get_account_stake(account_name: str, fallback: Any = None) -> float:
    """Returns dynamic stake configured for an account, falling back to custom_stake or global stake_per_trade."""
    settings = load_settings()
    stakes = settings.get("account_stakes", {})
    if account_name in stakes and stakes[account_name] is not None:
        try:
            return float(stakes[account_name])
        except (ValueError, TypeError):
            pass
    if fallback is not None:
        return float(fallback)
    return float(settings.get("stake_per_trade", 25.0))


def set_account_stake(account_name: str, stake: float) -> None:
    """Updates dynamic stake for a specific account."""
    settings = load_settings()
    stakes = dict(settings.get("account_stakes", {}))
    stakes[account_name] = float(stake)
    settings["account_stakes"] = stakes
    save_settings(settings)


# Per-account settings that can independently diverge from the global defaults above,
# so each configured trading account can run/pause and pursue its own strategy on its own risk budget.
ACCOUNT_OVERRIDABLE_KEYS = [
    "bot_status",              # "RUNNING" or "PAUSED" for this account only
    "entry_kill_switch",       # blocks new entries for this account only
    "max_open_positions",
    "max_total_exposure",
    "max_trades_per_day",
    "price_min",
    "price_max",
    "min_volume",
    "min_liquidity",
    "sports_market_types",
]


def get_account_settings(account_name: str) -> Dict[str, Any]:
    """Returns effective settings for one account: global settings overlaid with
    that account's own overrides, so each account can be independently paused,
    kill-switched, risk-capped, and strategy-filtered."""
    settings = load_settings()
    effective = {k: settings.get(k) for k in ACCOUNT_OVERRIDABLE_KEYS}
    # An account that has not set its own band must inherit the band actually in
    # force, not the raw price_min/price_max. Inheriting those meant that with Late
    # Game on, every signal inside the Late Game band was silently dropped by the
    # per-account filter for failing a price_min the mode does not even use.
    effective["price_min"], effective["price_max"] = effective_price_band(settings)
    overrides = settings.get("account_overrides", {}).get(account_name, {})
    for key, value in overrides.items():
        if key in ACCOUNT_OVERRIDABLE_KEYS and value is not None:
            effective[key] = value
    return effective


def set_account_override(account_name: str, key: str, value: Any) -> None:
    """Sets a single per-account override, independent of the global setting."""
    if key not in ACCOUNT_OVERRIDABLE_KEYS:
        raise ValueError(f"'{key}' is not a per-account overridable setting.")
    settings = load_settings()
    overrides = dict(settings.get("account_overrides", {}))
    acc_overrides = dict(overrides.get(account_name, {}))
    acc_overrides[key] = value
    overrides[account_name] = acc_overrides
    settings["account_overrides"] = overrides
    save_settings(settings)


def clear_account_override(account_name: str, key: str) -> None:
    """Removes a per-account override, reverting that key back to the global setting."""
    settings = load_settings()
    overrides = dict(settings.get("account_overrides", {}))
    acc_overrides = dict(overrides.get(account_name, {}))
    acc_overrides.pop(key, None)
    overrides[account_name] = acc_overrides
    settings["account_overrides"] = overrides
    save_settings(settings)
