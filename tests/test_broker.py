import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"] = "state.json"
os.environ["TRADES_DB_FILE"] = "trades.db"
os.environ["SETTINGS_FILE"] = "settings.json"
import paper_broker, database, config

class Opp:
    def __init__(self, tok, price=0.95):
        self.token_id = tok; self.market_id = "m"+tok; self.question = "Will X win?"
        self.outcome_label = "Yes"; self.confirmed_price = price; self.end_date = None
        self.slug = "x-slug"; self.market_type = "moneyline"; self.game_start_time = None

b = paper_broker.PaperBroker()

# 1. PAPER open debits the simulated balance
start_bal = b.state["balance"]
pos, why = b.open_position(Opp("111111111"), stake=25.0, mode="PAPER")
assert pos, why
assert abs(b.state["balance"] - (start_bal - 25.0)) < 1e-9, b.state["balance"]
print("PASS paper open debits balance")

# 2. LIVE open must NOT touch the simulated balance
bal_before = b.state["balance"]
pos2, why2 = b.open_position(Opp("222222222"), stake=25.0, mode="LIVE", account_name="Acc A")
assert pos2, why2
assert b.state["balance"] == bal_before, (b.state["balance"], bal_before)
print("PASS live open leaves paper balance alone")

# 3. Fill-aware sizing: partial fill produces a partial position
pos3, _ = b.open_position(Opp("333333333", 0.90), stake=50.0, mode="LIVE",
                          account_name="Acc A", filled_size=10.0, fill_price=0.91)
assert abs(pos3["shares"] - 10.0) < 1e-9, pos3["shares"]
assert abs(pos3["stake"] - 9.1) < 1e-9, pos3["stake"]
print("PASS position sized from actual fill, not the request")

# 4. trade_ids are unique across re-entry into the same market
ids = {p["trade_id"] for p in b.state["positions"].values()}
assert len(ids) == len(b.state["positions"]), ids
b.exit_position("111111111", 0.99, note="t")
pos4, _ = b.open_position(Opp("111111111"), stake=25.0, mode="PAPER")
rows = database.get_all_trades(limit=100)
assert len({r["trade_id"] for r in rows}) == len(rows), "trade_id collision"
assert len([r for r in rows if r["token_id"] == "111111111"]) == 2, "re-entry overwrote history"
print("PASS re-entering a market keeps both rows")

# 5. discard_position(void) clears state.json AND refunds a paper stake
bal_before = b.state["balance"]
assert b.discard_position(pos4["trade_id"], note="never filled") is True
assert b._find_position_key("111111111") is None, "void left the position in state.json"
assert abs(b.state["balance"] - (bal_before + 25.0)) < 1e-9
assert not any(t.get("trade_id") == pos4["trade_id"] for t in b.state["closed_trades"]), "void booked as a trade"
print("PASS void clears state.json, refunds paper stake, books no trade")

# 6. VOID rows stay out of the win rate
database.void_trade(pos4["trade_id"], note="never filled")
row = [r for r in database.get_all_trades(limit=100) if r["trade_id"] == pos4["trade_id"]][0]
assert row["result"] == "VOID" and row["pnl"] == 0.0, row
s = b.summary(mode_filter="PAPER")
assert all(str(t.get("result","")).upper() != "VOID" for t in b.state["closed_trades"])
print("PASS void_trade marks VOID with zero pnl")

# 7. A second process's write is not clobbered by a stale in-memory snapshot
b2 = paper_broker.PaperBroker()
b2.open_position(Opp("999999999"), stake=10.0, mode="PAPER")
b.save_signals([])                       # b's snapshot predates b2's write
on_disk = json.load(open("state.json"))
assert any(p["token_id"] == "999999999" for p in on_disk["positions"].values()), \
    "stale snapshot resurrected old state / dropped a concurrent write"
print("PASS concurrent writer's position survives the other broker's save")
print("\nall broker tests passed")

# 8. Reset clears PAPER only and keeps LIVE positions tracked
b3 = paper_broker.PaperBroker()
b3.open_position(Opp("444444444"), stake=5.0, mode="PAPER")
b3.open_position(Opp("555555555"), stake=5.0, mode="LIVE", account_name="Acc A")
live_before = len([p for p in b3.state["positions"].values() if str(p["mode"]).upper() == "LIVE"])
paper_before = len([p for p in b3.state["positions"].values() if str(p["mode"]).upper() == "PAPER"])
assert paper_before >= 1 and live_before >= 1, (paper_before, live_before)
kept = b3.reset_paper_portfolio()
assert kept == live_before, (kept, live_before)
modes = {str(p["mode"]).upper() for p in b3.state["positions"].values()}
assert modes == {"LIVE"}, modes
assert b3.state["balance"] == config.STARTING_BALANCE
print("PASS reset clears paper only, live positions kept")
