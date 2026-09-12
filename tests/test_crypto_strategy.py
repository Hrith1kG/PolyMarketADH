"""End-to-end behaviour of the Crypto 5-Minute strategy: entry, the pre-trade
re-check, and one-entry-per-round across repeated scans and restarts."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = tempfile.mkdtemp()
os.chdir(WORK)
os.environ["STATE_FILE"] = "state_cx.json"
os.environ["TRADES_DB_FILE"] = "trades_cx.db"
os.environ["SETTINGS_FILE"] = "settings_cx.json"

from datetime import timedelta

import crypto_markets as cm
import crypto_scanner
import crypto_strategy
import paper_broker
import settings_manager
from tests.crypto_fixtures import FakeBook, FakeClient, NOW, make_market

CRYPTO_ON = {
    "crypto_enabled": True,
    "crypto_entry_window_seconds": 30,
    "crypto_min_probability": 0.90,
    "crypto_stake_per_trade": 10.0,
    "crypto_max_trades_per_day": 50,
    "crypto_max_open_positions": 20,
    "crypto_max_total_exposure": 1000.0,
}


def reset_state():
    """Starts a scenario from an empty book and an empty round ledger."""
    for path in (os.environ["STATE_FILE"], os.environ["STATE_FILE"] + ".lock"):
        try:
            os.remove(path)
        except OSError:
            pass


def configure(reset=True, **overrides):
    if reset:
        reset_state()
    settings = dict(settings_manager.DEFAULT_SETTINGS)
    settings.update(CRYPTO_ON)
    settings.update(overrides)
    settings_manager.save_settings(settings)
    return settings


def fresh_broker():
    return paper_broker.PaperBroker()


def build(asset="btc", seconds_left=20, market_id="900",
          up=0.95, down=0.05, tokens=("r1-up", "r1-down")):
    market = make_market(asset=asset, seconds_left=seconds_left, market_id=market_id,
                         prices=(up, down), tokens=tokens)
    books = {
        tokens[0]: FakeBook(best_bid=up - 0.01, best_ask=up),
        tokens[1]: FakeBook(best_bid=max(down - 0.01, 0.001), best_ask=down),
    }
    return market, FakeClient([market], books)


def strategy(broker, client, logs=None):
    sink = logs if logs is not None else []
    return crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: None, log=sink.append
    ), sink


# 1. A qualifying Up round is entered once, at the re-checked executable ask.
configure()
broker = fresh_broker()
market, client = build()
strat, logs = strategy(broker, client)
result = strat.run_once(now=NOW)
assert result.entries == 1, (result, logs)
positions = list(broker.crypto_positions().values())
assert len(positions) == 1, positions
assert positions[0]["outcome_label"] == "Up"
assert abs(positions[0]["entry_price"] - 0.95) < 1e-9, positions[0]["entry_price"]
assert positions[0]["market_type"] == crypto_scanner.CRYPTO_MARKET_TYPE
assert client.market_calls and client.book_calls, "the entry must be re-checked against live data"
print("PASS a qualifying Up round is entered once at the re-checked ask")

# 2. Repeated scans inside the same window do NOT re-enter the same round.
for _ in range(5):
    again = strat.run_once(now=NOW)
    assert again.entries == 0, again
    assert "already_traded_this_round" in again.reasons, again.reasons
assert len(broker.crypto_positions()) == 1, broker.crypto_positions()
print("PASS repeated scans inside the same window never re-enter the round")

# 3. The claim survives a restart: a brand-new broker (re-reading state.json)
#    and a brand-new strategy still refuse the round.
restarted_broker = fresh_broker()
restart_strat, _ = strategy(restarted_broker, FakeClient([market], client.books))
after_restart = restart_strat.run_once(now=NOW)
assert after_restart.entries == 0, after_restart
assert "already_traded_this_round" in after_restart.reasons
assert len(restarted_broker.crypto_positions()) == 1
print("PASS the one-entry-per-round claim survives a restart")

# 4. A Down round is entered on the Down side only.
configure()
broker = fresh_broker()
market, client = build(asset="eth", market_id="901",
                       up=0.03, down=0.97, tokens=("r2-up", "r2-down"))
strat, logs = strategy(broker, client)
result = strat.run_once(now=NOW)
assert result.entries == 1, (result, logs)
pos = list(broker.crypto_positions().values())[0]
assert pos["outcome_label"] == "Down" and abs(pos["entry_price"] - 0.97) < 1e-9, pos
print("PASS a qualifying Down round is entered on the Down side")

# 5. A different round of the same market is a separate entry.
later_market = make_market(asset="eth", seconds_left=20 + 300,
                           market_id="901", prices=(0.03, 0.97), tokens=("r2b-up", "r2b-down"))
# The books are quoted at the later round's own clock, since quote freshness is
# one of the gates.
LATER = NOW + timedelta(seconds=300)
later_client = FakeClient([later_market], {
    "r2b-up": FakeBook(best_bid=0.02, best_ask=0.03, timestamp=LATER),
    "r2b-down": FakeBook(best_bid=0.96, best_ask=0.97, timestamp=LATER),
})
later_strat, _ = strategy(broker, later_client)
# The next round only becomes tradable once its own window opens.
assert later_strat.run_once(now=NOW).entries == 0
assert later_strat.run_once(now=LATER).entries == 1
assert len(broker.crypto_positions()) == 2, broker.crypto_positions()
print("PASS the next round of the same market is a separate, later entry")

# 6. Both 30s and 60s windows drive real entries at their own boundaries.
for window in (30, 60):
    configure(crypto_entry_window_seconds=window)
    broker = fresh_broker()
    market, client = build(asset="sol", market_id=f"91{window}",
                           seconds_left=window - 1, tokens=(f"w{window}-up", f"w{window}-down"))
    strat, logs = strategy(broker, client)
    assert strat.run_once(now=NOW).entries == 1, (window, logs)

    # One second too early is refused, and costs no order.
    broker2 = fresh_broker()
    market2, client2 = build(asset="sol", market_id=f"92{window}",
                             seconds_left=window + 1, tokens=(f"e{window}-up", f"e{window}-down"))
    strat2, _ = strategy(broker2, client2)
    res2 = strat2.run_once(now=NOW)
    assert res2.entries == 0 and cm.REASON_TOO_EARLY in res2.reasons, res2.reasons
print("PASS entries fire inside 30s and 60s windows and not one second before")

# 7. The re-check is authoritative: a round that expires between scan and order
#    is refused, the claim is given back, and nothing is bought.
configure()
broker = fresh_broker()
market, client = build(asset="xrp", market_id="930", seconds_left=2,
                       tokens=("r3-up", "r3-down"))
strat, logs = strategy(broker, client)
result = strat.run_once(now=NOW + timedelta(seconds=2))   # scan time is inside, submit time is not
assert result.entries == 0, result
assert broker.crypto_positions() == {}, broker.crypto_positions()
assert broker.claimed_crypto_round_keys() == set(), "a refused entry must release its claim"
print("PASS a round that expires before submission is refused and its claim released")

# 8. A price that falls below the floor between scan and order is refused too.
configure()
broker = fresh_broker()
market, client = build(asset="doge", market_id="940",
                       tokens=("r4-up", "r4-down"))
strat, logs = strategy(broker, client)


class DecayingClient(FakeClient):
    """Serves a qualifying book to the scan and a decayed one to the re-check.

    The re-check is the only thing that re-reads the market, so `market_calls`
    is an exact marker for "we are now past the scan".
    """

    def get_order_book(self, token_id):
        if token_id == "r4-up" and self.market_calls:
            return FakeBook(best_bid=0.80, best_ask=0.82)
        return super().get_order_book(token_id)


decaying = DecayingClient([market], client.books)
strat, logs = strategy(broker, decaying)
result = strat.run_once(now=NOW)
assert result.entries == 0, (result, logs)
assert cm.REASON_PRICE_BELOW_THRESHOLD in result.reasons, result.reasons
assert broker.claimed_crypto_round_keys() == set()
print("PASS a price that decays before submission is refused at the re-check")

# 9. Slippage: an ask that runs up past the cap between scan and order is refused.
configure(crypto_max_slippage=0.01)
broker = fresh_broker()
market, client = build(asset="btc", market_id="950",
                       up=0.92, down=0.08, tokens=("r5-up", "r5-down"))


class RunawayClient(FakeClient):
    """Qualifying at 0.92 during the scan, 0.98 by the time the order is sent."""

    def get_order_book(self, token_id):
        if token_id == "r5-up" and self.market_calls:
            return FakeBook(best_bid=0.97, best_ask=0.98)
        return super().get_order_book(token_id)


strat, logs = strategy(broker, RunawayClient([market], client.books))
result = strat.run_once(now=NOW)
assert result.entries == 0 and cm.REASON_SLIPPAGE in result.reasons, (result.reasons, logs)
print("PASS an ask that runs past the slippage cap before submission is refused")

# 10. The crypto kill switch and pause block crypto entries.
for key, value in [("crypto_entry_kill_switch", True), ("crypto_bot_status", "PAUSED")]:
    configure(**{key: value})
    broker = fresh_broker()
    market, client = build(asset="btc", market_id="960",
                           tokens=("r6-up", "r6-down"))
    strat, _ = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 0, (key, result)
    assert broker.crypto_positions() == {}
print("PASS the crypto kill switch and crypto pause both block crypto entries")

# 11. The crypto risk budget is enforced and is its own, separate from sports.
configure(crypto_max_open_positions=1, crypto_max_trades_per_day=50)
broker = fresh_broker()
m1, c1 = build(asset="btc", market_id="970", tokens=("r7-up", "r7-down"))
s1, _ = strategy(broker, c1)
assert s1.run_once(now=NOW).entries == 1
m2, c2 = build(asset="eth", market_id="971", tokens=("r8-up", "r8-down"))
s2, _ = strategy(broker, c2)
res = s2.run_once(now=NOW)
assert res.entries == 0 and cm.REASON_RISK_BLOCKED in res.reasons, res.reasons
assert broker.claimed_crypto_round_keys() == {cm.classify_market(m1).round_key}
print("PASS the crypto open-position cap blocks a second round and releases its claim")

configure(crypto_max_trades_per_day=1)
broker = fresh_broker()
m1, c1 = build(asset="btc", market_id="980", tokens=("r9-up", "r9-down"))
s1, _ = strategy(broker, c1)
assert s1.run_once(now=NOW).entries == 1
assert broker.crypto_trades_today() == 1
m2, c2 = build(asset="sol", market_id="981", tokens=("r10-up", "r10-down"))
s2, _ = strategy(broker, c2)
assert s2.run_once(now=NOW).entries == 0
print("PASS the crypto daily trade cap is counted and enforced separately")

# 12. With the strategy disabled, nothing is scanned at all.
configure(crypto_enabled=False)
broker = fresh_broker()
market, client = build(asset="btc", market_id="990", tokens=("r11-up", "r11-down"))
strat, _ = strategy(broker, client)
result = strat.run_once(now=NOW)
assert result.entries == 0 and client.list_calls == [], client.list_calls
print("PASS a disabled crypto strategy issues no requests at all")

# 13. Skip logs name the reason for every refused round.
configure()
broker = fresh_broker()
noise = [
    make_market(asset="btc", seconds_left=200, market_id="1100", tokens=("f-up", "f-down")),
    make_market(asset="eth", seconds_left=10, market_id="1101", tokens=("c-up", "c-down"), prices=(0.60, 0.40)),
]
client = FakeClient(noise, {
    "f-up": FakeBook(best_bid=0.94, best_ask=0.95), "f-down": FakeBook(best_ask=0.05),
    "c-up": FakeBook(best_bid=0.59, best_ask=0.60), "c-down": FakeBook(best_bid=0.39, best_ask=0.40),
})
strat, logs = strategy(broker, client)
strat.run_once(now=NOW)
joined = "\n".join(logs)
assert cm.REASON_TOO_EARLY in joined, joined
assert cm.REASON_PRICE_BELOW_THRESHOLD in joined, joined
print("PASS skipped rounds are logged with a machine-readable reason")

# 14. LIVE execution: the entry is sized from the real fill, and an order that
#     merely rests in the book still consumes the round's single entry.
class FakeSession:
    def __init__(self, name):
        self.name = name


class FakeLiveBroker:
    """Stands in for LiveBroker: records what was ordered, returns a scripted result."""

    def __init__(self, outcome="filled"):
        self.outcome = outcome
        self.orders = []

    def get_session(self, name):
        return FakeSession(name)

    def place_buy_selected(self, token_id, price, account_stakes, order_type="LIMIT",
                           max_price=None):
        self.orders.append({"token_id": token_id, "price": price,
                            "stakes": dict(account_stakes), "order_type": order_type,
                            "max_price": max_price})
        results = []
        for name, stake in account_stakes.items():
            if self.outcome == "filled":
                results.append({
                    "account_name": name, "wallet": "0xabc", "stake": stake, "success": True,
                    "accepted": True, "resting": False, "filled_size": 9.0,
                    "filled_cost": 9.0 * price, "avg_price": price, "status": "matched",
                    "order_id": "ord-1", "error": None,
                })
            elif self.outcome == "resting":
                results.append({
                    "account_name": name, "wallet": "0xabc", "stake": stake, "success": False,
                    "accepted": True, "resting": True, "filled_size": 0.0, "filled_cost": 0.0,
                    "avg_price": 0.0, "status": "live", "order_id": "ord-2",
                    "error": "Order accepted but unfilled",
                })
            else:
                results.append({
                    "account_name": name, "wallet": "0xabc", "stake": stake, "success": False,
                    "accepted": False, "resting": False, "filled_size": 0.0, "filled_cost": 0.0,
                    "avg_price": 0.0, "status": "unmatched", "order_id": None,
                    "error": "Order rejected by exchange",
                })
        return results


import config as _config

_ACCOUNTS = [{"id": "1", "name": "Acc A", "private_key": "k" * 64, "funder_address": None,
              "stake": 10.0, "enabled": True, "relayer_api_key": None,
              "relayer_api_key_address": None}]
_config.get_configured_accounts = lambda: list(_ACCOUNTS)


def live_run(outcome, asset, market_id, tokens):
    configure(live_trading=True)
    broker = fresh_broker()
    _market, client = build(asset=asset, market_id=market_id, tokens=tokens)
    live = FakeLiveBroker(outcome=outcome)
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None
    )
    return broker, live, strat.run_once(now=NOW)


broker, live, result = live_run("filled", "btc", "1200", ("L1-up", "L1-down"))
assert result.entries == 1, result
# The exchange-side price cap documented for market orders is passed through:
# our own re-check only sees the book as it was a moment ago.
assert live.orders == [{"token_id": "L1-up", "price": 0.95, "stakes": {"Acc A": 10.0},
                        "order_type": "LIMIT",
                        "max_price": 0.95 + settings_manager.DEFAULT_SETTINGS["crypto_max_slippage"]}], live.orders
pos = list(broker.crypto_positions().values())[0]
assert pos["mode"] == "LIVE" and pos["account_name"] == "Acc A"
assert abs(pos["shares"] - 9.0) < 1e-9, pos["shares"]   # sized from the fill, not the request
print("PASS a live entry orders once per eligible account and books the real fill")

broker, live, result = live_run("resting", "eth", "1201", ("L2-up", "L2-down"))
assert result.entries == 1, result
assert broker.crypto_positions() == {}, "a resting order is not a position"
assert broker.claimed_crypto_round_keys(), "a resting order must keep the round claimed"
print("PASS a resting live order books no position but still consumes the round")

broker, live, result = live_run("rejected", "sol", "1202", ("L3-up", "L3-down"))
assert result.entries == 0, result
assert broker.claimed_crypto_round_keys() == set(), "a rejected order must release the claim"
print("PASS a rejected live order releases the round for a later retry")


# 15. A filled order whose local bookkeeping blows up must still consume the
#     round: the shares are already on the exchange, and releasing the claim
#     here would let the next poll buy them a second time.
configure(live_trading=True)
broker = fresh_broker()
_market, client = build(asset="xrp", market_id="1203", tokens=("L4-up", "L4-down"))
live = FakeLiveBroker(outcome="filled")


def _explode(*args, **kwargs):
    raise RuntimeError("database is locked")


broker.open_position = _explode
strat = crypto_strategy.CryptoStrategy(
    broker, client=client, live_provider=lambda: live, log=lambda m: None
)
result = strat.run_once(now=NOW)
assert result.entries == 1, result
assert broker.claimed_crypto_round_keys(), "a filled order must keep the round claimed"
assert len(live.orders) == 1, live.orders
print("PASS a fill that fails to book locally still consumes the round")


print("\nALL crypto_strategy TESTS PASSED")
