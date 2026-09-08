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
    "max_order_notional": 25.0,  # Max order size in USD
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
    "late_game_enabled": False,
    "require_authoritative_time": False,
    "late_game_threshold_seconds": 600,
}


def load_settings() -> Dict[str, Any]:
    """Loads current settings from JSON, filling in any missing defaults."""
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
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
