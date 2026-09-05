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
