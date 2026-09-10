import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

ENV_FILE = os.getenv("ENV_FILE", ".env")


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

# Single canonical fallback account label. Used whenever a trade/position is recorded
# without an explicit account_name (the dashboard's manual PAPER trade button, main.py's
# PAPER fallback loop, and the single-account .env fallback below) so all of these paths
# agree on one string instead of "Primary" vs "Primary Account" silently diverging in the
# trades table and account filters.
DEFAULT_ACCOUNT_NAME = "Primary"


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
                "name": DEFAULT_ACCOUNT_NAME,
                "private_key": pk,
                "funder_address": funder or None,
                "stake": stake,
                "enabled": True,
                "relayer_api_key": relayer_k or None,
                "relayer_api_key_address": relayer_a or None,
            })

    return accounts


# --- Runtime account management (writes to .env so accounts persist across restarts) ---

def _read_env_lines(path: str = None) -> list:
    path = path or ENV_FILE
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return f.readlines()


def _write_env_lines(lines: list, path: str = None) -> None:
    path = path or ENV_FILE
    tmp = f"{path}.tmp"
    # This file holds private keys. Create it 0600 before writing so the keys are
    # never briefly readable by other users on the machine (the previous version
    # wrote at the process umask, typically 0644).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best effort: some filesystems (e.g. Windows shares) don't support it


def _set_env_var(lines: list, key: str, value: str) -> list:
    """Sets KEY=value in-place if present, otherwise appends it. Returns the updated lines."""
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}={value}\n"
            return lines
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{key}={value}\n")
    return lines


def _next_account_slot(path: str = None) -> int:
    used = set()
    for line in _read_env_lines(path):
        stripped = line.strip()
        if stripped.startswith("ACCOUNT_") and "_PRIVATE_KEY=" in stripped:
            try:
                used.add(int(stripped.split("_")[1]))
            except (ValueError, IndexError):
                pass
    for i in range(1, 21):
        if i not in used:
            return i
    raise ValueError("Maximum of 20 accounts already configured.")


def add_account(
    name: str,
    private_key: str,
    funder_address: str = "",
    stake: Optional[float] = None,
    relayer_api_key: str = "",
    relayer_api_key_address: str = "",
    path: str = None,
) -> int:
    """Adds a new numbered trading account by writing ACCOUNT_N_* entries to .env.
    Returns the slot index it was assigned. Raises ValueError on invalid input."""
    private_key = (private_key or "").strip()
    if not private_key or "your_" in private_key.lower() or len(private_key) < 32:
        raise ValueError("A valid private key is required (min 32 characters).")

    idx = _next_account_slot(path)
    lines = _read_env_lines(path)
    lines = _set_env_var(lines, f"ACCOUNT_{idx}_NAME", (name or "").strip() or f"Account_{idx}")
    lines = _set_env_var(lines, f"ACCOUNT_{idx}_PRIVATE_KEY", private_key)
    lines = _set_env_var(lines, f"ACCOUNT_{idx}_FUNDER_ADDRESS", (funder_address or "").strip())
    lines = _set_env_var(lines, f"ACCOUNT_{idx}_ENABLED", "true")
    if stake is not None:
        lines = _set_env_var(lines, f"ACCOUNT_{idx}_STAKE", str(stake))
    if relayer_api_key:
        lines = _set_env_var(lines, f"ACCOUNT_{idx}_RELAYER_API_KEY", relayer_api_key.strip())
    if relayer_api_key_address:
        lines = _set_env_var(lines, f"ACCOUNT_{idx}_RELAYER_API_KEY_ADDRESS", relayer_api_key_address.strip())
    _write_env_lines(lines, path)
    return idx


def set_account_enabled(account_id: str, enabled: bool, path: str = None) -> None:
    """Enables or disables a configured account without deleting its credentials."""
    lines = _read_env_lines(path)
    lines = _set_env_var(lines, f"ACCOUNT_{account_id}_ENABLED", "true" if enabled else "false")
    _write_env_lines(lines, path)


def remove_account(account_id: str, path: str = None) -> None:
    """Permanently removes a configured account's entries (including its private key) from .env."""
    prefix = f"ACCOUNT_{account_id}_"
    lines = [l for l in _read_env_lines(path) if not l.strip().startswith(prefix)]
    _write_env_lines(lines, path)


def set_account_relayer(account_id: str, relayer_api_key: str, relayer_api_key_address: str, path: str = None) -> None:
    """Adds or updates an existing account's Polymarket Relayer API credentials (for
    gasless order submission) without touching its private key or any other field.
    Passing empty strings clears the account's relayer config, falling back to the
    global RELAYER_API_KEY/RELAYER_API_KEY_ADDRESS (if set) or direct/gas-paying
    submission."""
    lines = _read_env_lines(path)
    lines = _set_env_var(lines, f"ACCOUNT_{account_id}_RELAYER_API_KEY", (relayer_api_key or "").strip())
    lines = _set_env_var(lines, f"ACCOUNT_{account_id}_RELAYER_API_KEY_ADDRESS", (relayer_api_key_address or "").strip())
    _write_env_lines(lines, path)

