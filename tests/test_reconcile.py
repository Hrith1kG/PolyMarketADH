import os, sys, types, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"]="state3.json"; os.environ["TRADES_DB_FILE"]="trades3.db"; os.environ["SETTINGS_FILE"]="settings3.json"
for f in ("state3.json","trades3.db","settings3.json"):
    if os.path.exists(f): os.remove(f)
import paper_broker, database, live_broker

class Opp:
    def __init__(s, tok, price=0.95):
        s.token_id=tok; s.market_id="m"+tok; s.question="Q "+tok; s.outcome_label="Yes"
        s.confirmed_price=price; s.end_date=None; s.slug=""; s.market_type="moneyline"; s.game_start_time=None

b = paper_broker.PaperBroker()
p_phantom,_ = b.open_position(Opp("700000001"), stake=1.0, mode="LIVE", account_name="Anubrata 2")
p_sold,_    = b.open_position(Opp("700000002"), stake=1.0, mode="LIVE", account_name="Anubrata 2")
p_held,_    = b.open_position(Opp("700000003"), stake=1.0, mode="LIVE", account_name="Anubrata 2")
assert len(b.state["positions"]) == 3

class Trade:
    def __init__(s, tok, side, price=None, size=None):
        s.asset_id=tok; s.side=side; s.price=price; s.size=size; s.timestamp="2026-09-10T00:00:00Z"
class Pager:
    def __init__(s, items): s.items=items
    def iter_items(s): return iter(s.items)

sess = types.SimpleNamespace()
sess.name = "Anubrata 2"
sess.wallet = "0xwallet"
# On-chain: only token ...003 is actually held.
sess.get_live_positions = lambda: [{"token_id": "700000003", "size": 1.05}]
# On-chain trade history: ...002 was bought then sold; ...001 never appears at all.
sess.client = types.SimpleNamespace(list_trades=lambda **kw: Pager([
    Trade("700000002", "BUY", 0.95, 1.05),
    Trade("700000002", "SELL", 0.99, 1.05),
    Trade("700000003", "BUY", 0.95, 1.05),
]))

lb = live_broker.LiveBroker.__new__(live_broker.LiveBroker)
lb.account_list = [sess]
lb.sessions = {"Anubrata 2": sess}
lb.get_session = lambda name=None: sess

out = lb.reconcile_positions()
by_tok = {r["trade_id"]: r for r in out}

# 1. The never-filled position is VOIDED, not settled as a win
v = by_tok[p_phantom["trade_id"]]
assert v["voided"] is True and v["pnl"] == 0.0, v
row = [r for r in database.get_all_trades(limit=50) if r["trade_id"] == p_phantom["trade_id"]][0]
assert row["result"] == "VOID" and row["pnl"] == 0.0, row
print("PASS never-filled position is VOIDED with zero P&L (was: booked as a WIN)")

# 2. ...and it is gone from state.json, so it stops rendering under Open Positions
b2 = paper_broker.PaperBroker()
assert b2._find_position_key("700000001") is None, "phantom still in state.json"
print("PASS voided position removed from state.json, not just trades.db")

# 3. The genuinely sold position settles at the real fill price
sold = by_tok[p_sold["trade_id"]]
assert abs(sold["exit_price"] - 0.99) < 1e-9, sold
assert abs(sold["pnl"] - (1.05*0.99 - 1.0)) < 1e-6, sold
assert b2._find_position_key("700000002") is None
print(f"PASS sold position settles at real exit ${sold['exit_price']:.2f}, pnl {sold['pnl']:+.4f}")

# 4. The still-held position is untouched
assert p_held["trade_id"] not in by_tok, "held position was wrongly reconciled"
assert b2._find_position_key("700000003") is not None, "held position was dropped"
print("PASS still-held position left alone")

# 5. VOID rows stay out of the win rate
s = b2.summary(mode_filter="LIVE")
assert s["closed_trades"] == 1 and s["wins"] == 1, s
assert s["win_rate"] == 100.0, s
print(f"PASS win rate computed over 1 real trade, not 2 (void excluded)")
print("\nall reconcile tests passed")
