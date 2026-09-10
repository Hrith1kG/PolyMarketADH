import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"]="state2.json"; os.environ["TRADES_DB_FILE"]="trades2.db"; os.environ["SETTINGS_FILE"]="settings2.json"
from live_broker import interpret_order_response
from polymarket.models.clob.order_response import RawOrderResponse, normalize_order_response

def resp(**kw):
    base = dict(errorMsg="", makingAmount="0", orderID="0xabc", status="live", success=True, takingAmount="0")
    base.update(kw)
    return normalize_order_response(RawOrderResponse.parse_response(base))

# 1. Outright rejection: no exception is raised by the SDK, so this MUST not read as a fill
from live_broker import is_no_balance_rejection
r = interpret_order_response(resp(success=False, errorMsg="not enough balance / allowance", status="unmatched"),
                             side="BUY", requested_size=10, requested_price=0.95)
assert r["filled"] is False and r["filled_size"] == 0.0, r
# The SDK infers "unmatched" from the status even for a balance failure, so the
# no-balance check has to look at the message too.
assert is_no_balance_rejection(r["code"], r["message"]), r
assert is_no_balance_rejection("not_enough_balance", ""), "explicit code must match"
assert not is_no_balance_rejection("unmatched", "no orders found to match"), "must not over-match"
print("PASS rejected order is not a fill, and reads as a no-balance rejection")

# 2. Unmatched
r = interpret_order_response(resp(success=False, errorMsg="", status="unmatched"),
                             side="BUY", requested_size=10, requested_price=0.95)
assert r["filled"] is False and r["code"] == "unmatched", r
print("PASS unmatched order is not a fill")

# 3. Accepted but RESTING in the book: not a position
r = interpret_order_response(resp(status="live"), side="BUY", requested_size=10, requested_price=0.95)
assert r["accepted"] and not r["filled"] and r["resting"], r
print("PASS resting order is accepted but not filled")

# 4. Genuine full fill, sized from the response
r = interpret_order_response(resp(status="matched", makingAmount="9.50", takingAmount="10"),
                             side="BUY", requested_size=10, requested_price=0.95)
assert r["filled"] and r["filled_size"] == 10.0 and r["filled_cost"] == 9.5, r
assert abs(r["avg_price"] - 0.95) < 1e-9, r
print("PASS matched buy reports 10 shares for $9.50")

# 5. Partial fill on a resting order: book only what filled
r = interpret_order_response(resp(status="live", makingAmount="3.80", takingAmount="4"),
                             side="BUY", requested_size=10, requested_price=0.95)
assert r["filled"] and r["filled_size"] == 4.0 and r["resting"], r
print("PASS partial fill books 4 of 10 shares and flags the remainder")

# 6. SELL side: making/taking swap meaning
r = interpret_order_response(resp(status="matched", makingAmount="10", takingAmount="9.80"),
                             side="SELL", requested_size=10, requested_price=0.98)
assert r["filled_size"] == 10.0 and r["filled_cost"] == 9.8, r
print("PASS sell fill reads 10 shares for $9.80 proceeds")

# 7. Matched but amounts omitted -> fall back, but flag it
r = interpret_order_response(resp(status="matched"), side="BUY", requested_size=7, requested_price=0.9)
assert r["filled"] and r["filled_size"] == 7.0 and r["amounts_unavailable"], r
print("PASS matched-without-amounts falls back and flags for reconciliation")
print("\nall order tests passed")
