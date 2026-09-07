import os
from dotenv import load_dotenv

load_dotenv()


def _float(name, default):
    return float(os.getenv(name, default))


def _int(name, default):
    return int(os.getenv(name, default))


def _bool(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- "Sureshot" filter ---
# A market outcome counts as a candidate when its price sits in [PRICE_MIN, PRICE_MAX].
# Below PRICE_MIN it isn't "almost confirmed" enough; above PRICE_MAX there's essentially
# no upside left to justify the tail risk of it flipping.
PRICE_MIN = _float("PRICE_MIN", 0.97)
PRICE_MAX = _float("PRICE_MAX", 0.995)

# Liquidity/volume guards: thin markets can show a fake "0.98" from one stale trade.
MIN_VOLUME = _float("MIN_VOLUME", 5000)
MIN_LIQUIDITY = _float("MIN_LIQUIDITY", 1000)

# Skip markets resolving too soon (higher chance of a last-second flip/dispute)
# or too far out (capital sits idle / more time for conditions to change).
MIN_HOURS_TO_RESOLUTION = _float("MIN_HOURS_TO_RESOLUTION", 1)
MAX_DAYS_TO_RESOLUTION = _float("MAX_DAYS_TO_RESOLUTION", 30)

# --- Position sizing / risk caps ---
STAKE_PER_TRADE = _float("STAKE_PER_TRADE", 25)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 10)
MAX_TOTAL_EXPOSURE = _float("MAX_TOTAL_EXPOSURE", 200)
STARTING_BALANCE = _float("STARTING_BALANCE", 1000)

# --- Loop / Timing ---
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)

# --- Execution mode ---
LIVE_TRADING = _bool("LIVE_TRADING", False)
PRIVATE_KEY = os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS") or os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
RELAYER_API_KEY = os.getenv("RELAYER_API_KEY") or os.getenv("POLYMARKET_RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS = os.getenv("RELAYER_API_KEY_ADDRESS") or os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "")

STATE_FILE = os.getenv("STATE_FILE", "state.json")


def get_configured_accounts() -> list:
    """Discovers and parses configured trading accounts from environment variables.
    Supports multi-account (ACCOUNT_1_..., ACCOUNT_2_...) format while maintaining
    100% backward compatibility with single-account (PRIVATE_KEY, FUNDER_ADDRESS).
    """
    accounts = []

    # 1. Scan for numbered accounts: ACCOUNT_1_*, ACCOUNT_2_*, ...
    for i in range(1, 21):
        pk = (os.getenv(f"ACCOUNT_{i}_PRIVATE_KEY") or "").strip()
        if not pk:
            continue
        if "your_" in pk.lower() or len(pk) < 32:
            continue

        name = (os.getenv(f"ACCOUNT_{i}_NAME") or f"Account_{i}").strip()
        funder = (os.getenv(f"ACCOUNT_{i}_FUNDER_ADDRESS") or "").strip()
        custom_stake_str = os.getenv(f"ACCOUNT_{i}_STAKE")
        try:
            stake = float(custom_stake_str) if custom_stake_str else STAKE_PER_TRADE
        except ValueError:
            stake = STAKE_PER_TRADE
        enabled = _bool(f"ACCOUNT_{i}_ENABLED", True)
        relayer_key = (os.getenv(f"ACCOUNT_{i}_RELAYER_API_KEY") or RELAYER_API_KEY).strip()
        relayer_addr = (os.getenv(f"ACCOUNT_{i}_RELAYER_API_KEY_ADDRESS") or RELAYER_API_KEY_ADDRESS).strip()

        accounts.append({
            "id": str(i),
            "name": name,
            "private_key": pk,
            "funder_address": funder or None,
            "stake": stake,
            "enabled": enabled,
            "relayer_api_key": relayer_key or None,
            "relayer_api_key_address": relayer_addr or None,
        })

    # 2. Fallback to single account if no numbered accounts configured
    if not accounts:
        pk = (os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY") or PRIVATE_KEY or "").strip()
        funder = (os.getenv("FUNDER_ADDRESS") or os.getenv("POLYMARKET_FUNDER_ADDRESS") or FUNDER_ADDRESS or "").strip()
        custom_stake_str = os.getenv("STAKE_PER_TRADE")
        try:
            stake = float(custom_stake_str) if custom_stake_str else STAKE_PER_TRADE
        except ValueError:
            stake = STAKE_PER_TRADE
        relayer_k = (os.getenv("RELAYER_API_KEY") or RELAYER_API_KEY or "").strip()
        relayer_a = (os.getenv("RELAYER_API_KEY_ADDRESS") or RELAYER_API_KEY_ADDRESS or "").strip()

        if pk and "your_private_key" not in pk.lower() and len(pk) >= 32:
            accounts.append({
                "id": "1",
                "name": "Primary Account",
                "private_key": pk,
                "funder_address": funder or None,
                "stake": stake,
                "enabled": True,
                "relayer_api_key": relayer_k or None,
                "relayer_api_key_address": relayer_a or None,
            })

    return accounts

