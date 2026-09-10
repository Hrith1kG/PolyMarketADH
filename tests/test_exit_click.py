import os, sys, json, tempfile
os.environ["STATE_FILE"]="state_x.json"; os.environ["TRADES_DB_FILE"]="trades_x.db"
os.environ["SETTINGS_FILE"]="settings_x.json"; os.environ["ENV_FILE"]="x.env"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(tempfile.mkdtemp())
for f in ("state_x.json","trades_x.db","settings_x.json"):
    if os.path.exists(f): os.remove(f)

json.dump({
    "balance": 1000.0,
    "positions": {"55555555": {
        "trade_id": "trd_x_1", "mode": "PAPER", "account_name": "Primary",
        "token_id": "55555555", "market_id": "mx", "question": "Will Hull City AFC win?",
        "outcome_label": "No", "entry_price": 0.94, "shares": 10.0, "stake": 9.4,
        "time_left": "2.0h", "slug": ""}},
    "closed_trades": [], "signals": [],
    "daily_trades": {"date": "2026-09-10", "count": 1},
    "order_lifecycle": {"intentions": 1, "pending": 0, "filled": 1, "rejected": 0},
    "logs": [],
}, open("state_x.json","w"))
json.dump({"bot_status": "PAUSED", "live_trading": False}, open("settings_x.json","w"))

import database
database.record_trade({"trade_id":"trd_x_1","placed_at":"2026-09-10T00:00:00","market_id":"mx",
    "token_id":"55555555","slug":"","question":"Will Hull City AFC win?","outcome":"No",
    "entry_price":0.94,"tokens":10.0,"cost":9.4,"time_left":"2.0h","result":"PENDING",
    "resolved_price":None,"payout":None,"pnl":0.0,"broker":"paper","tx_hash":None,
    "closed_at":None,"note":"","account_name":"Primary","wallet_address":""})

from streamlit.testing.v1 import AppTest
at = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
at.run()
assert not at.exception, [str(e.value) for e in at.exception]

btns = {b.key: b for b in at.button}
key = "ov_btn_exit_55555555"
assert key in btns, sorted(k for k in btns if k and "exit" in k)
btns[key].click().run()
assert not at.exception, [str(e.value) for e in at.exception]

# Both stores must agree that the position is closed.
state = json.load(open("state_x.json"))
assert "55555555" not in state["positions"], "position still in state.json after exit"
row = [r for r in database.get_all_trades(limit=10) if r["trade_id"] == "trd_x_1"][0]
assert row["result"] != "PENDING", row
assert len(state["closed_trades"]) == 1, state["closed_trades"]
print(f"PASS exit clears state.json AND trades.db together (result={row['result']}, pnl={row['pnl']:+.2f})")

at2 = AppTest.from_file(os.path.join(REPO, "dashboard.py"), default_timeout=120)
at2.run()
assert not at2.exception, [str(e.value) for e in at2.exception]
assert not any(b.key == "ov_btn_exit_55555555" for b in at2.button), \
    "exited position still rendered under Open Positions"
print("PASS exited position no longer rendered under Open Positions")
