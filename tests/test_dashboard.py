import os, sys, tempfile
os.environ["STATE_FILE"]="state_dash.json"; os.environ["TRADES_DB_FILE"]="trades_dash.db"
os.environ["SETTINGS_FILE"]="settings_dash.json"; os.environ["ENV_FILE"]="dash.env"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(tempfile.mkdtemp())

# Seed a state that reproduces the reported screenshot: LIVE positions tracked
# locally, paused bot, no credentials configured.
import json
json.dump({
    "balance": 1000.0,
    "positions": {
        "Anubrata2_88888888": {
            "trade_id": "trd_ord_Anubrata2_88888888_1", "mode": "LIVE",
            "account_name": "Anubrata 2", "token_id": "88888888", "market_id": "m1",
            "question": "Will England win?", "outcome_label": "Yes", "entry_price": 0.99,
            "shares": 1.01, "stake": 1.0, "time_left": "3.0h", "slug": "",
            "settlement_blocked": "Deadline passed but outcome unconfirmed",
        }
    },
    "closed_trades": [], "signals": [],
    "daily_trades": {"date": "2026-09-10", "count": 1},
    "order_lifecycle": {"intentions": 5, "pending": 2, "filled": 1, "rejected": 2},
    "logs": [],
}, open("state_dash.json", "w"))
json.dump({"bot_status": "PAUSED", "live_trading": True}, open("settings_dash.json", "w"))

from streamlit.testing.v1 import AppTest
at = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
at.run()
if at.exception:
    for e in at.exception:
        print("EXCEPTION:", e.value)
        print(e.stack_trace if hasattr(e, "stack_trace") else "")
    sys.exit(1)
print("PASS dashboard renders with no exceptions")
print("  tabs:", len(at.tabs))
print("  warnings:", [w.value[:90] for w in at.warning])
# The blocked-settlement notice must be visible
assert any("could not be confirmed" in w.value for w in at.warning), \
    "unresolvable position was not surfaced"
print("PASS unresolvable position surfaced instead of silently auto-settled")
