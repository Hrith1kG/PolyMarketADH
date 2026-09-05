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
    "bot_status": "RUNNING",     # "RUNNING" or "PAUSED"
    "manual_scan_requested": False,
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
