"""End-to-end behaviour of the Crypto 5-Minute strategy: entry, the pre-trade
re-check, and one-entry-per-round across repeated scans and restarts."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = tempfile.mkdtemp()
os.chdir(WORK)
STATE_FILE = os.path.join(WORK, "state_cx.json")
TRADES_DB_FILE = os.path.join(WORK, "trades_cx.db")
SETTINGS_FILE = os.path.join(WORK, "settings_cx.json")
os.environ["STATE_FILE"] = STATE_FILE
os.environ["TRADES_DB_FILE"] = TRADES_DB_FILE
os.environ["SETTINGS_FILE"] = SETTINGS_FILE

from datetime import timedelta
from decimal import Decimal

import config as _config
_config.STATE_FILE = STATE_FILE
_config.SETTINGS_FILE = SETTINGS_FILE

import database
database.DB_FILE = TRADES_DB_FILE

import crypto_markets as cm
import crypto_scanner
import crypto_strategy
import paper_broker
import settings_manager
settings_manager.SETTINGS_FILE = SETTINGS_FILE

from tests.crypto_fixtures import FakeBook, FakeClient, NOW, make_market, Trading

CRYPTO_ON = {
    "crypto_enabled": True,
    "crypto_entry_window_seconds": 30,
    "crypto_min_probability": 0.90,
    "crypto_stake_per_trade": 10.0,
    "crypto_max_trades_per_day": 50,
    "crypto_max_open_positions": 20,
    "crypto_max_total_exposure": 1000.0,
}

_ACCOUNTS = [{"id": "1", "name": "Acc A", "private_key": "k" * 64, "funder_address": None,
              "stake": 10.0, "enabled": True, "relayer_api_key": None,
              "relayer_api_key_address": None}]
_config.get_configured_accounts = lambda: list(_ACCOUNTS)


def reset_state():
    """Starts a scenario from an empty book and an empty round ledger."""
    for path in (STATE_FILE, STATE_FILE + ".lock"):
        try:
            os.remove(path)
        except OSError:
            pass
    database.init_db(TRADES_DB_FILE, force=True)
    try:
        with database.get_connection(TRADES_DB_FILE) as conn:
            conn.execute("DELETE FROM trades")
            conn.commit()
    except Exception:
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
    return paper_broker.PaperBroker(state_path=STATE_FILE)


def build(asset="btc", seconds_left=20, market_id="900",
          up=0.95, down=0.05, tokens=("r1-up", "r1-down"),
          best_bid=None, trading=None, question=None):
    market = make_market(asset=asset, seconds_left=seconds_left, market_id=market_id,
                         prices=(up, down), tokens=tokens, trading=trading, question=question)
    bid_0 = best_bid if best_bid is not None else (up - 0.01)
    books = {
        tokens[0]: FakeBook(best_bid=bid_0, best_ask=up),
        tokens[1]: FakeBook(best_bid=max(down - 0.01, 0.001), best_ask=down),
    }
    return market, FakeClient([market], books)


def strategy(broker, client, logs=None, live_provider=None, sleep_fn=None, binance_client=None):
    sink = logs if logs is not None else []
    prov = live_provider if live_provider is not None else (lambda: None)
    slp = sleep_fn if sleep_fn is not None else (lambda s: None)
    return crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=prov, log=sink.append, sleep_fn=slp,
        binance_client=binance_client,
    ), sink


class FakeSession:
    def __init__(self, name):
        self.name = name


class FakeLiveBroker:
    """Stands in for LiveBroker: records what was ordered, returns a scripted result."""

    def __init__(self, outcome="filled", open_orders=None, cancel_callback=None, orders_by_id=None):
        self.outcome = outcome
        self.orders = []
        self.cancelled_orders = []
        self.open_orders = list(open_orders) if open_orders is not None else []
        self.cancel_callback = cancel_callback
        self.orders_by_id = dict(orders_by_id) if orders_by_id is not None else {}

    def get_session(self, name):
        return FakeSession(name)

    def get_open_orders(self, account_name=None):
        return list(self.open_orders)

    def get_order(self, order_id, account_name=None):
        if str(order_id) in self.orders_by_id:
            val = self.orders_by_id[str(order_id)]
            return dict(val) if isinstance(val, dict) else val
        for o in self.open_orders:
            o_id = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            if str(o_id) == str(order_id):
                return dict(o) if isinstance(o, dict) else o
        return None

    def cancel_order(self, order_id, account_name=None):
        self.cancelled_orders.append((order_id, account_name))
        if self.cancel_callback:
            self.cancel_callback(order_id, account_name)
        return {"status": "cancelled", "order_id": order_id}

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


def live_run(outcome, asset, market_id, tokens, live_broker_instance=None, **settings_overrides):
    configure(live_trading=True, **settings_overrides)
    broker = fresh_broker()
    _market, client = build(asset=asset, market_id=market_id, tokens=tokens)
    live = live_broker_instance if live_broker_instance is not None else FakeLiveBroker(outcome=outcome)
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    return broker, live, strat.run_once(now=NOW)


# --- Unit Tests ---

def test_qualifying_up_round():
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


def test_repeated_scans_inside_window():
    # 2. Repeated scans inside the same window do NOT re-enter the same round.
    configure()
    broker = fresh_broker()
    market, client = build()
    strat, logs = strategy(broker, client)
    res = strat.run_once(now=NOW)
    assert res.entries == 1
    for _ in range(5):
        again = strat.run_once(now=NOW)
        assert again.entries == 0, again
        assert "already_traded_this_round" in again.reasons, again.reasons
    assert len(broker.crypto_positions()) == 1, broker.crypto_positions()
    print("PASS repeated scans inside the same window never re-enter the round")


def test_claim_survives_restart():
    # 3. The claim survives a restart: a brand-new broker (re-reading state.json)
    #    and a brand-new strategy still refuse the round.
    configure()
    broker = fresh_broker()
    market, client = build()
    strat, logs = strategy(broker, client)
    assert strat.run_once(now=NOW).entries == 1

    restarted_broker = fresh_broker()
    restart_strat, _ = strategy(restarted_broker, FakeClient([market], client.books))
    after_restart = restart_strat.run_once(now=NOW)
    assert after_restart.entries == 0, after_restart
    assert "already_traded_this_round" in after_restart.reasons
    assert len(restarted_broker.crypto_positions()) == 1
    print("PASS the one-entry-per-round claim survives a restart")


def test_qualifying_down_round():
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


def test_different_round_same_market():
    # 5. A different round of the same market is a separate entry.
    configure()
    broker = fresh_broker()
    market, client = build(asset="eth", market_id="901",
                           up=0.03, down=0.97, tokens=("r2-up", "r2-down"))
    strat, _ = strategy(broker, client)
    assert strat.run_once(now=NOW).entries == 1

    later_market = make_market(asset="eth", seconds_left=20 + 300,
                               market_id="901", prices=(0.03, 0.97), tokens=("r2b-up", "r2b-down"))
    LATER = NOW + timedelta(seconds=300)
    later_client = FakeClient([later_market], {
        "r2b-up": FakeBook(best_bid=0.02, best_ask=0.03, timestamp=LATER),
        "r2b-down": FakeBook(best_bid=0.96, best_ask=0.97, timestamp=LATER),
    })
    later_strat, _ = strategy(broker, later_client)
    assert later_strat.run_once(now=NOW).entries == 0
    assert later_strat.run_once(now=LATER).entries == 1
    assert len(broker.crypto_positions()) == 2, broker.crypto_positions()
    print("PASS the next round of the same market is a separate, later entry")


def test_entry_windows_boundaries():
    # 6. Both 30s and 60s windows drive real entries at their own boundaries.
    for window in (30, 60):
        configure(crypto_entry_window_seconds=window)
        broker = fresh_broker()
        market, client = build(asset="sol", market_id=f"91{window}",
                               seconds_left=window - 1, tokens=(f"w{window}-up", f"w{window}-down"))
        strat, logs = strategy(broker, client)
        assert strat.run_once(now=NOW).entries == 1, (window, logs)

        broker2 = fresh_broker()
        market2, client2 = build(asset="sol", market_id=f"92{window}",
                                 seconds_left=window + 1, tokens=(f"e{window}-up", f"e{window}-down"))
        strat2, _ = strategy(broker2, client2)
        res2 = strat2.run_once(now=NOW)
        assert res2.entries == 0 and cm.REASON_TOO_EARLY in res2.reasons, res2.reasons
    print("PASS entries fire inside 30s and 60s windows and not one second before")


def test_recheck_round_expired():
    # 7. The re-check is authoritative: a round that expires between scan and order
    #    is refused, the claim is given back, and nothing is bought.
    configure()
    broker = fresh_broker()
    market, client = build(asset="xrp", market_id="930", seconds_left=2,
                           tokens=("r3-up", "r3-down"))
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW + timedelta(seconds=2))
    assert result.entries == 0, result
    assert broker.crypto_positions() == {}, broker.crypto_positions()
    assert broker.claimed_crypto_round_keys() == set(), "a refused entry must release its claim"
    print("PASS a round that expires before submission is refused and its claim released")


def test_recheck_price_decay():
    # 8. A price that falls below the floor between scan and order is refused too.
    configure()
    broker = fresh_broker()
    market, client = build(asset="doge", market_id="940",
                           tokens=("r4-up", "r4-down"))
    class DecayingClient(FakeClient):
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


def test_recheck_slippage():
    # 9. Slippage: an ask that runs up past the cap between scan and order is refused.
    configure(crypto_max_slippage=0.01)
    broker = fresh_broker()
    market, client = build(asset="btc", market_id="950",
                           up=0.92, down=0.08, tokens=("r5-up", "r5-down"))
    class RunawayClient(FakeClient):
        def get_order_book(self, token_id):
            if token_id == "r5-up" and self.market_calls:
                return FakeBook(best_bid=0.97, best_ask=0.98)
            return super().get_order_book(token_id)

    strat, logs = strategy(broker, RunawayClient([market], client.books))
    result = strat.run_once(now=NOW)
    assert result.entries == 0 and cm.REASON_SLIPPAGE in result.reasons, (result.reasons, logs)
    print("PASS an ask that runs past the slippage cap before submission is refused")


def test_kill_switch_and_pause():
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


def test_risk_budget_open_positions():
    # 11. The crypto risk budget is enforced: open positions limit
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


def test_risk_budget_daily_trade_cap():
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


def test_disabled_strategy():
    # 12. With the strategy disabled, nothing is scanned at all.
    configure(crypto_enabled=False)
    broker = fresh_broker()
    market, client = build(asset="btc", market_id="990", tokens=("r11-up", "r11-down"))
    strat, _ = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 0 and client.list_calls == [], client.list_calls
    print("PASS a disabled crypto strategy issues no requests at all")


def test_skipped_rounds_reasons():
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


def test_live_execution_fills_and_cancels():
    # 14. LIVE execution: fill, resting maker cancellation, and rejection
    broker, live, result = live_run("filled", "btc", "1200", ("L1-up", "L1-down"))
    assert result.entries == 1, result
    assert live.orders == [{"token_id": "L1-up", "price": 0.95, "stakes": {"Acc A": 10.0},
                            "order_type": "LIMIT",
                            "max_price": 0.95 + settings_manager.DEFAULT_SETTINGS["crypto_max_slippage"]}], live.orders
    pos = list(broker.crypto_positions().values())[0]
    assert pos["mode"] == "LIVE" and pos["account_name"] == "Acc A"
    assert abs(pos["shares"] - 9.0) < 1e-9, pos["shares"]
    print("PASS a live entry orders once per eligible account and books the real fill")

    broker, live, result = live_run("resting", "eth", "1201", ("L2-up", "L2-down"))
    assert result.entries == 1, result
    assert broker.crypto_positions() == {}, "a resting order is not a position"
    assert broker.claimed_crypto_round_keys(), "a resting order must keep the round claimed"
    assert ("ord-2", "Acc A") in live.cancelled_orders, "resting maker order must be cancelled by auto-cancel"
    print("PASS a resting live order books no position but still consumes the round and is cancelled")

    broker, live, result = live_run("rejected", "sol", "1202", ("L3-up", "L3-down"))
    assert result.entries == 0, result
    assert broker.claimed_crypto_round_keys() == set(), "a rejected order must release the claim"
    print("PASS a rejected live order releases the round for a later retry")


def test_fill_failing_bookkeeping():
    # 15. A filled order whose local bookkeeping blows up must still consume the round
    configure(live_trading=True)
    broker = fresh_broker()
    _market, client = build(asset="xrp", market_id="1203", tokens=("L4-up", "L4-down"))
    live = FakeLiveBroker(outcome="filled")

    def _explode(*args, **kwargs):
        raise RuntimeError("database is locked")

    broker.open_position = _explode
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    result = strat.run_once(now=NOW)
    assert result.entries == 1, result
    assert broker.claimed_crypto_round_keys(), "a filled order must keep the round claimed"
    assert len(live.orders) == 1, live.orders
    print("PASS a fill that fails to book locally still consumes the round")


def test_maker_order_pricing():
    # 16. Maker Order Pricing: Limit order submitted at best_bid + 0.01 when crypto_maker_mode is True.
    configure(crypto_maker_mode=True)
    broker = fresh_broker()
    # best_bid = 0.92, best_ask = 0.96 -> maker limit price is exactly 0.93 (best_bid + 0.01)
    market, client = build(asset="btc", market_id="1300", up=0.96, best_bid=0.92, tokens=("m1-up", "m1-down"))
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 1, (result, logs)
    pos = list(broker.crypto_positions().values())[0]
    assert abs(pos["entry_price"] - 0.93) < 1e-9, f"expected entry_price 0.93, got {pos['entry_price']}"
    print("PASS maker order limit price is exactly best_bid + 0.01 (0.93 vs ask 0.96)")


def test_maker_mode_disabled():
    # 17. Maker Mode Disabled: When crypto_maker_mode is False, order takes the best_ask.
    configure(crypto_maker_mode=False)
    broker = fresh_broker()
    market, client = build(asset="btc", market_id="1301", up=0.96, best_bid=0.92, tokens=("m2-up", "m2-down"))
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 1, (result, logs)
    pos = list(broker.crypto_positions().values())[0]
    assert abs(pos["entry_price"] - 0.96) < 1e-9, f"expected entry_price 0.96 (taker ask), got {pos['entry_price']}"
    print("PASS taker order placed at best_ask when crypto_maker_mode is False")


def test_maker_tick_size_rounding():
    # 18. Tick size rounding: Maker price snaps down to market minimum tick size.
    configure(crypto_maker_mode=True)
    broker = fresh_broker()
    # best_bid = 0.90, best_bid + 0.01 = 0.91, but minimum tick size is 0.05 -> snaps down to 0.90
    market, client = build(asset="eth", market_id="1302", up=0.95, best_bid=0.90,
                           tokens=("m3-up", "m3-down"), trading=Trading(minimum_tick_size=0.05))
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 1, (result, logs)
    pos = list(broker.crypto_positions().values())[0]
    assert abs(pos["entry_price"] - 0.90) < 1e-9, f"expected 0.90 on 0.05 tick size, got {pos['entry_price']}"
    print("PASS maker order price respects market minimum tick size")


def test_auto_cancel_safety_net():
    # 19. Auto-Cancel Safety Net: Waits crypto_maker_cancel_seconds and calls cancel_order.
    slept_durations = []
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="sol", market_id="1303", tokens=("m4-up", "m4-down"))
    live = FakeLiveBroker(outcome="resting")
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 0.0}]
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=slept_durations.append,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1
    assert slept_durations == [4], f"expected sleep [4], got {slept_durations}"
    assert ("ord-2", "Acc A") in live.cancelled_orders, f"cancel_order was not called: {live.cancelled_orders}"
    assert broker.crypto_positions() == {}, "unfilled cancelled order must not book a position"
    print("PASS auto-cancel safety net waits configured delay and calls cancel_order")


def test_partial_fills_strategy():
    # 20. Partial Fills Strategy: Partially filled maker order books the filled portion after cancel.
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="doge", market_id="1304", up=0.95, best_bid=0.94, tokens=("m5-up", "m5-down"))
    live = FakeLiveBroker(outcome="resting")
    # Order was for 10.0 shares, 4.0 shares filled while resting
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 4.0, "price": 0.95}]
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1, res
    assert ("ord-2", "Acc A") in live.cancelled_orders, "cancel_order must be called for open remainder"
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1, positions
    pos = positions[0]
    assert abs(pos["shares"] - 4.0) < 1e-9, f"expected 4.0 shares, got {pos['shares']}"
    assert abs(pos["stake"] - (4.0 * 0.95)) < 1e-9, f"expected stake {4.0 * 0.95}, got {pos['stake']}"
    assert abs(pos["entry_price"] - 0.95) < 1e-9, pos["entry_price"]
    print("PASS partial fill accurately tracked and booked after auto-cancel triggers")


def test_full_fill_during_wait_window():
    # 21. Full fill during wait window: cancel_order is NOT called if order fully filled.
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="xrp", market_id="1305", up=0.95, best_bid=0.94, tokens=("m6-up", "m6-down"))
    live = FakeLiveBroker(outcome="resting")
    # Order was for 10.0 shares, fully filled (10.0 matched) while resting
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 10.0, "price": 0.95}]
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1, res
    assert live.cancelled_orders == [], f"cancel_order should not be called on full fill: {live.cancelled_orders}"
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1
    assert abs(positions[0]["shares"] - 10.0) < 1e-9
    print("PASS fully filled maker order books full position without cancelling")


def test_maker_full_fill_not_in_open_orders():
    # 22. Exchange scenario: Once fully matched, order is no longer in open_orders.
    # get_order(order_id) retrieves the order record directly with status="matched".
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="btc", market_id="1307", up=0.95, best_bid=0.94, tokens=("m8-up", "m8-down"))
    live = FakeLiveBroker(outcome="resting")
    live.open_orders = []  # No longer in open orders!
    live.orders_by_id = {
        "ord-2": {"id": "ord-2", "size": 10.0, "filled": 10.0, "price": 0.95, "status": "matched"}
    }
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1, res
    assert live.cancelled_orders == [], f"cancel_order should not be called on full fill: {live.cancelled_orders}"
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1, positions
    assert abs(positions[0]["shares"] - 10.0) < 1e-9
    assert abs(positions[0]["stake"] - 9.5) < 1e-9
    print("PASS maker order fully matched on exchange (not in open orders) is booked correctly")


def test_maker_price_capped_at_max_limit():
    # 23. Maker price must not exceed maximum valid limit price (1.0 - tick_size, e.g. 0.99)
    configure(crypto_maker_mode=True)
    broker = fresh_broker()
    market, client = build(asset="btc", market_id="1308", up=0.995, best_bid=0.99, tokens=("m9-up", "m9-down"))
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 1, (result, logs)
    pos = list(broker.crypto_positions().values())[0]
    assert pos["entry_price"] <= 0.99, f"maker price must not exceed 0.99, got {pos['entry_price']}"
    print("PASS maker order price capped below 1.00 on Polymarket limit order grid")


def test_maker_fallback_when_no_bid():
    # 24. Maker order falls back to best_ask when no bid exists on book
    configure(crypto_maker_mode=True, crypto_require_two_sided_book=False)
    broker = fresh_broker()
    market = make_market(asset="btc", seconds_left=20, market_id="1309", prices=(0.95, 0.05),
                         tokens=("m10-up", "m10-down"))
    books = {
        "m10-up": FakeBook(best_ask=0.95, one_sided="no_bids"),
        "m10-down": FakeBook(best_bid=0.04, best_ask=0.05),
    }
    client = FakeClient([market], books)
    strat, logs = strategy(broker, client)
    result = strat.run_once(now=NOW)
    assert result.entries == 1, (result, logs)
    pos = list(broker.crypto_positions().values())[0]
    assert abs(pos["entry_price"] - 0.95) < 1e-9, f"expected best_ask 0.95, got {pos['entry_price']}"
    print("PASS maker order falls back to best_ask when no bid exists")


def test_exception_safety():
    # 25. Exception Safety: Core execution does not crash if cancel_order raises an exception.
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="btc", market_id="1306", tokens=("m7-up", "m7-down"))
    live = FakeLiveBroker(outcome="resting")
    def failing_cancel(order_id, account_name=None):
        raise RuntimeError("Exchange network timeout during cancel")
    live.cancel_callback = failing_cancel
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1
    print("PASS execution flow does not crash when cancel_order raises an exception")


def test_auto_cancel_zero_seconds():
    # 26. Zero seconds cancel delay: cancels immediately without sleeping
    slept_durations = []
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=0)
    broker = fresh_broker()
    _m, client = build(asset="btc", market_id="1310", tokens=("m11-up", "m11-down"))
    live = FakeLiveBroker(outcome="resting")
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 0.0}]
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=slept_durations.append,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1
    assert slept_durations == [], f"expected no sleep, got {slept_durations}"
    assert ("ord-2", "Acc A") in live.cancelled_orders, "cancel_order must be called"
    print("PASS zero-second auto-cancel cancels immediately without sleeping")


def test_order_matched_during_cancel():
    # 27. Concurrent fill: order is unfilled before cancel, but matches during the cancel request.
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="sol", market_id="1311", up=0.95, best_bid=0.94, tokens=("m12-up", "m12-down"))
    live = FakeLiveBroker(outcome="resting")
    # Pre-cancel lookup shows unfilled resting
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 0.0, "price": 0.95}]
    # When cancel is called, it simulates the exchange matching the order just before cancel took effect
    def cancel_hook(order_id, account_name=None):
        live.orders_by_id["ord-2"] = {"id": "ord-2", "size": 10.0, "filled": 10.0, "price": 0.95, "status": "matched"}
    live.cancel_callback = cancel_hook
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1, res
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1, "concurrently matched order must be booked"
    assert abs(positions[0]["shares"] - 10.0) < 1e-9
    assert abs(positions[0]["stake"] - 9.5) < 1e-9
    print("PASS concurrent fill during cancel request is detected and booked as full fill")


def test_crypto_settings_robust_parsing():
    # 28. Robust parsing of maker_mode and maker_cancel_seconds from strings or floats
    s1 = crypto_scanner.crypto_settings({"crypto_maker_mode": "false", "crypto_maker_cancel_seconds": "5.0"})
    assert s1["maker_mode"] is False, s1["maker_mode"]
    assert s1["maker_cancel_seconds"] == 5, s1["maker_cancel_seconds"]

    s2 = crypto_scanner.crypto_settings({"crypto_maker_mode": "true", "crypto_maker_cancel_seconds": 2})
    assert s2["maker_mode"] is True, s2["maker_mode"]
    assert s2["maker_cancel_seconds"] == 2, s2["maker_cancel_seconds"]
    print("PASS crypto_settings robustly parses string and float maker configurations")


def test_account_session_get_order_shapes():
    # 29. AccountSession.get_order handles both dict and object returns from client
    import live_broker
    session = live_broker.AccountSession.__new__(live_broker.AccountSession)
    session.name = "TestAcc"
    session.wallet = "0xTest"
    class MockClient:
        def __init__(self, mode):
            self.mode = mode
        def get_order(self, order_id):
            if self.mode == "dict":
                return {
                    "id": order_id, "market": "m1", "asset_id": "tok1", "side": "BUY",
                    "price": 0.95, "original_size": 10.0, "size_matched": 4.0, "status": "LIVE"
                }
            elif self.mode == "obj":
                class ObjOrder:
                    id = order_id
                    market = "m1"
                    asset_id = "tok1"
                    side = "BUY"
                    price = 0.95
                    original_size = 10.0
                    size_matched = 4.0
                    status = "LIVE"
                return ObjOrder()
            return None

    session.client = MockClient("dict")
    ord_dict = session.get_order("ord-dict")
    assert ord_dict is not None
    assert ord_dict["id"] == "ord-dict" and ord_dict["filled"] == 4.0 and ord_dict["size"] == 10.0

    session.client = MockClient("obj")
    ord_obj = session.get_order("ord-obj")
    assert ord_obj is not None
    assert ord_obj["id"] == "ord-obj" and ord_obj["filled"] == 4.0 and ord_obj["size"] == 10.0
    print("PASS AccountSession.get_order parses both dict and object shapes cleanly")


def test_maker_order_object_shape():
    # 30. Object shape handling: live.get_order returning an object instance
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=2)
    broker = fresh_broker()
    _m, client = build(asset="btc", market_id="1312", up=0.96, best_bid=0.92, tokens=("m13-up", "m13-down"))
    live = FakeLiveBroker(outcome="resting")
    
    class OrderModel:
        def __init__(self, id, size, filled, status):
            self.id = id
            self.size = size
            self.filled = filled
            self.status = status

    live.orders_by_id["ord-2"] = OrderModel("ord-2", 10.0, 10.0, "matched")
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1, res
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1, "object shape order must be booked without AttributeError"
    assert abs(positions[0]["shares"] - 10.0) < 1e-9
    print("PASS live.get_order returning object shape books properly without AttributeError")


def test_auto_cancel_failure_leaves_order_resting():
    # 31. When cancel_order fails, order is kept as resting=True and not marked cancelled
    configure(live_trading=True, crypto_maker_mode=True, crypto_maker_cancel_seconds=4)
    broker = fresh_broker()
    _m, client = build(asset="eth", market_id="1313", tokens=("m14-up", "m14-down"))
    live = FakeLiveBroker(outcome="resting")
    live.open_orders = [{"id": "ord-2", "size": 10.0, "filled": 0.0, "status": "live"}]
    
    def fail_cancel(order_id, account_name=None):
        raise RuntimeError("CLOB cancel 503 service unavailable")
    live.cancel_callback = fail_cancel

    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None,
    )
    res = strat.run_once(now=NOW)
    assert res.entries == 1
    # Check that broker has unfilled intention recorded and round stays claimed
    assert broker.claimed_crypto_round_keys(), "failed cancel must keep the round claimed"
    assert broker.crypto_positions() == {}, "unfilled order must not be a position"
    print("PASS cancel_order failure preserves resting state and keeps round claimed")


def test_maker_price_above_ceiling_rejected():
    # 32. If best_bid + 0.01 exceeds crypto_max_probability, recheck rejects with REASON_PRICE_ABOVE_CEILING
    configure(crypto_maker_mode=True, crypto_max_probability=0.95)
    broker = fresh_broker()
    # ask is 0.95 (within ceiling), but best_bid is 0.95 -> maker price would be 0.96 > 0.95 ceiling
    market, client = build(asset="btc", market_id="1314", up=0.95, best_bid=0.95, tokens=("m15-up", "m15-down"))
    strat, logs = strategy(broker, client)
    res = strat.run_once(now=NOW)
    assert res.entries == 0
    assert cm.REASON_PRICE_ABOVE_CEILING in res.reasons, res.reasons
    print("PASS maker price above configured max_probability ceiling is rejected")


def test_account_session_get_order_positional_and_open_orders_objects():
    # 33. AccountSession.get_order with positional client call and object items in get_open_orders
    import live_broker
    session = live_broker.AccountSession.__new__(live_broker.AccountSession)
    session.name = "TestAcc2"
    session.wallet = "0xPositional"
    
    class PositionalClient:
        def get_order(self, order_id):
            # Positional parameter only, no keyword 'order_id'
            class ObjOrder:
                id = order_id
                asset_id = "tok_pos"
                side = "BUY"
                price = 0.93
                size = 15.0
                filled = 7.5
                status = "live"
            return ObjOrder()
            
    session.client = PositionalClient()
    ord_info = session.get_order("ord-pos")
    assert ord_info is not None
    assert ord_info["id"] == "ord-pos" and ord_info["size"] == 15.0 and ord_info["filled"] == 7.5
    assert ord_info["price"] == 0.93
    print("PASS AccountSession.get_order handles positional client call and object attributes")


def test_negative_cancel_seconds_floored():
    # 34. Negative maker_cancel_seconds is floored to 0
    s = crypto_scanner.crypto_settings({"crypto_maker_cancel_seconds": -5})
    assert s["maker_cancel_seconds"] == 0, s["maker_cancel_seconds"]
    print("PASS negative maker_cancel_seconds is floored to 0")


def test_binance_symbol_normalization():
    # 35. Binance symbol normalization for approved assets and custom tickers
    import binance_client
    assert binance_client.to_binance_symbol("BTC") == "BTCUSDT"
    assert binance_client.to_binance_symbol("btc") == "BTCUSDT"
    assert binance_client.to_binance_symbol("bitcoin") == "BTCUSDT"
    assert binance_client.to_binance_symbol("ETH") == "ETHUSDT"
    assert binance_client.to_binance_symbol("ethereum") == "ETHUSDT"
    assert binance_client.to_binance_symbol("SOL") == "SOLUSDT"
    assert binance_client.to_binance_symbol("XRP") == "XRPUSDT"
    assert binance_client.to_binance_symbol("DOGE") == "DOGEUSDT"
    assert binance_client.to_binance_symbol("BTCUSDT") == "BTCUSDT"
    assert binance_client.to_binance_symbol("avax") == "AVAXUSDT"
    assert binance_client.to_binance_symbol("ETH/USDT") == "ETHUSDT"
    assert binance_client.to_binance_symbol("BTC-USDT") == "BTCUSDT"
    assert binance_client.to_binance_symbol("BTC/USD") == "BTCUSDT"
    assert binance_client.to_binance_symbol("ETH-USD") == "ETHUSDT"
    assert binance_client.to_binance_symbol("SOLUSD") == "SOLUSDT"
    # USDC pairs and alternative asset names
    assert binance_client.to_binance_symbol("BTC/USDC") == "BTCUSDT"
    assert binance_client.to_binance_symbol("ETH-USDC") == "ETHUSDT"
    assert binance_client.to_binance_symbol("SOLUSDC") == "SOLUSDT"
    assert binance_client.to_binance_symbol("Avalanche") == "AVAXUSDT"
    assert binance_client.to_binance_symbol("Binance Coin") == "BNBUSDT"
    assert binance_client.to_binance_symbol("Chainlink") == "LINKUSDT"
    assert binance_client.to_binance_symbol("Cardano") == "ADAUSDT"
    assert binance_client.to_binance_symbol("Polygon") == "MATICUSDT"
    assert binance_client.to_binance_symbol("POL") == "POLUSDT"
    assert binance_client.to_binance_symbol("SUI") == "SUIUSDT"
    assert binance_client.to_binance_symbol("PEPE") == "PEPEUSDT"
    assert binance_client.to_binance_symbol("Shiba Inu") == "SHIBUSDT"
    assert binance_client.to_binance_symbol("USDT") == ""
    assert binance_client.to_binance_symbol("USDC") == ""
    assert binance_client.to_binance_symbol("USD") == ""
    print("PASS Binance symbol normalization maps assets to USDT tickers")


def test_binance_client_rest_mock():
    # 36. BinanceClient fetches price from REST endpoint and handles network/HTTP errors & US fallback
    import binance_client
    import urllib.request
    import urllib.error

    client = binance_client.BinanceClient()

    class MockResp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    orig_urlopen = urllib.request.urlopen

    # Success case
    def mock_urlopen_success(req, *args, **kwargs):
        assert "BTCUSDT" in req.full_url
        return MockResp(200, b'{"symbol":"BTCUSDT","price":"58123.50"}')

    urllib.request.urlopen = mock_urlopen_success
    try:
        price = client.get_spot_price("BTC")
        assert price == 58123.50
    finally:
        urllib.request.urlopen = orig_urlopen

    # US Fallback case (when global api.binance.com is geo-blocked with HTTP 451)
    def mock_urlopen_fallback(req, *args, **kwargs):
        if "api.binance.com" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 451, "Unavailable For Legal Reasons", {}, None)
        if "api.binance.us" in req.full_url:
            return MockResp(200, b'{"symbol":"BTCUSDT","price":"58150.00"}')
        raise urllib.error.URLError("Not reachable")

    urllib.request.urlopen = mock_urlopen_fallback
    try:
        price = client.get_spot_price("BTC")
        assert price == 58150.00
    finally:
        urllib.request.urlopen = orig_urlopen

    # Error cases: HTTP error, timeout, malformed json
    def mock_urlopen_err(req, *args, **kwargs):
        raise urllib.error.URLError("Connection refused")

    urllib.request.urlopen = mock_urlopen_err
    try:
        assert client.get_spot_price("BTC") is None
    finally:
        urllib.request.urlopen = orig_urlopen

    def mock_urlopen_bad_json(req, *args, **kwargs):
        return MockResp(200, b'not json')

    urllib.request.urlopen = mock_urlopen_bad_json
    try:
        assert client.get_spot_price("BTC") is None
    finally:
        urllib.request.urlopen = orig_urlopen
    print("PASS BinanceClient fetches and parses spot price cleanly with error resilience and US fallback")


def test_strike_price_extraction_various_assets():
    # 37. Strike price parsing works across different crypto assets and question formats
    assert cm.extract_strike_price("BTC > $58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC > $58,010.50 at 12:00 PM?") == 58010.5
    assert cm.extract_strike_price("ETH > $2,450 at 1:30 PM?") == 2450.0
    assert cm.extract_strike_price("ETH > $2,450.75 at 1:30 PM?") == 2450.75
    assert cm.extract_strike_price("SOL > $135.50 at 2:00 PM?") == 135.5
    assert cm.extract_strike_price("DOGE > $0.1250 at 5:00 PM?") == 0.125
    assert cm.extract_strike_price("XRP > $0.5850 at 12:00 PM?") == 0.585
    assert cm.extract_strike_price("Bitcoin > $64,200 at 3:15 PM ET?") == 64200.0
    assert cm.extract_strike_price("Ethereum > $3,100 at 4:00 PM?") == 3100.0
    assert cm.extract_strike_price("BTC > 58010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC >= $58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("Will BTC be above $58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("Will ETH price exceed $2,500 by 12:00 PM?") == 2500.0
    assert cm.extract_strike_price("BTC < $58,010 at 12:00 PM?") == 58010.0
    # Multipliers and suffixes (k/K, m/M, b/B, t/T)
    assert cm.extract_strike_price("BTC > $95k at 12:00 PM?") == 95000.0
    assert cm.extract_strike_price("BTC > 95k at 12:00 PM?") == 95000.0
    assert cm.extract_strike_price("ETH > $2.5k at 1:30 PM?") == 2500.0
    assert cm.extract_strike_price("Bitcoin > $100K at 5:00 PM?") == 100000.0
    assert cm.extract_strike_price("Will BTC reach $100k?") == 100000.0
    assert cm.extract_strike_price("Will Bitcoin hit $95k?") == 95000.0
    assert cm.extract_strike_price("Will BTC touch 90k before midnight?") == 90000.0
    assert cm.extract_strike_price("Total Cap > $2.5T") == 2_500_000_000_000.0
    assert cm.extract_strike_price("Volume > 1.5B USD") == 1_500_000_000.0
    # Scientific notation support
    assert cm.extract_strike_price("PEPE > 1.5e-5 at 12:00 PM?") == 1.5e-5
    assert cm.extract_strike_price("SHIB > 1.2e-4 at 12:00 PM?") == 1.2e-4
    assert cm.extract_strike_price("BTC > 5.8e4 at 12:00 PM?") == 58000.0
    # Additional currencies (€, £, ¥, ₹) and unicode operators (≥, ≤)
    assert cm.extract_strike_price("BTC > €58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("ETH > £2,450.50 at 1:30 PM?") == 2450.5
    assert cm.extract_strike_price("Will BTC settle at €58,010?") == 58010.0
    assert cm.extract_strike_price("Will BTC close above £2,450.50?") == 2450.5
    assert cm.extract_strike_price("BTC ≥ 58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC ≤ 58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC > 58010USDT at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC > 58,010 USD at 12:00 PM?") == 58010.0
    # Time/date collision safety (time-of-day numbers must NOT corrupt strike parsing)
    assert cm.extract_strike_price("Will BTC reach 5 PM above $58,000?") == 58000.0
    assert cm.extract_strike_price("Will BTC reach 12:00 PM above 58,000?") == 58000.0
    assert cm.extract_strike_price("BTC at 12:00 PM > 58,010?") == 58010.0
    # Directional verbs with and without $
    assert cm.extract_strike_price("BTC drops to 58,000 at 12:00 PM?") == 58000.0
    assert cm.extract_strike_price("BTC falls to 58,000 at 12:00 PM?") == 58000.0
    assert cm.extract_strike_price("BTC climbs to 60,000 at 12:00 PM?") == 60000.0
    assert cm.extract_strike_price("BTC rises to 60,000 at 12:00 PM?") == 60000.0
    assert cm.extract_strike_price("BTC dips to 58,000 at 12:00 PM?") == 58000.0
    assert cm.extract_strike_price("BTC surpasses 60,000 at 12:00 PM?") == 60000.0
    assert cm.extract_strike_price("BTC drops below 58,000 at 12:00 PM?") == 58000.0
    assert cm.extract_strike_price("BTC falls under 58,000 at 12:00 PM?") == 58000.0
    # HTML entities from web APIs (including double-escaped entities)
    assert cm.extract_strike_price("BTC &gt; $58,010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC &gt; 58010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC &lt; 58010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("BTC &amp;gt; 58010 at 12:00 PM?") == 58010.0
    assert cm.extract_strike_price("ETH &gt; $2.5k") == 2500.0
    # Hyphenated / slug formats
    assert cm.extract_strike_price("btc-above-58010-12pm") == 58010.0
    # Dictionary and grouped market extraction via get_market_strike_price
    assert cm.get_market_strike_price({"strike_price": 58010.0}) == 58010.0
    assert cm.get_market_strike_price({"strikePrice": 58010.0}) == 58010.0
    assert cm.get_market_strike_price({"strike": 58010.0}) == 58010.0
    assert cm.get_market_strike_price({"question": "Bitcoin Price on Sep 13", "groupItemTitle": "> $58,010"}) == 58010.0
    assert cm.get_market_strike_price({"question": "Ethereum Price", "subtitle": "Will ETH exceed €2,500?"}) == 2500.0
    # Boundary and non-physical values
    assert cm.extract_strike_price("BTC > $0") is None
    assert cm.extract_strike_price("BTC > -100") is None
    assert cm.extract_strike_price(0.0) is None
    assert cm.extract_strike_price(-50.0) is None
    assert cm.extract_strike_price(58010.0) == 58010.0
    # Negative cases
    assert cm.extract_strike_price("BTC Up or Down") is None
    assert cm.extract_strike_price("") is None
    assert cm.extract_strike_price(None) is None
    assert cm.extract_strike_price("Will it rain in New York?") is None
    print("PASS strike price extraction succeeds across formats and asset questions")


def test_binance_stoploss_up_aborted_when_spot_below_strike():
    # 38. UP trade aborted when Binance spot price is below the strike price
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1401", up=0.95, down=0.05,
        tokens=("b1-up", "b1-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    mock_binance = {"BTC": 58000.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 0, res
    assert cm.REASON_BINANCE_STOPLOSS in res.reasons, res.reasons
    assert broker.crypto_positions() == {}, "No position should be opened when spot < strike for UP"
    assert broker.claimed_crypto_round_keys() == set(), "Aborted trade must release round claim"
    print("PASS buying UP is aborted when Binance spot price is below strike price")


def test_binance_stoploss_up_allowed_when_spot_above_strike():
    # 39. UP trade allowed when Binance spot price is at or above the strike price
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1402", up=0.95, down=0.05,
        tokens=("b2-up", "b2-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    mock_binance = {"BTC": 58050.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 1, (res, logs)
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1
    assert positions[0]["outcome_label"] == "Up"
    print("PASS buying UP succeeds when Binance spot price is above strike price")


def test_binance_stoploss_down_aborted_when_spot_above_strike():
    # 40. DOWN trade aborted when Binance spot price is above the strike price
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="eth", market_id="1403", up=0.03, down=0.97,
        tokens=("b3-up", "b3-down"),
        question="ETH > $2,450 at 1:30 PM?",
    )
    mock_binance = {"ETH": 2455.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 0, res
    assert cm.REASON_BINANCE_STOPLOSS in res.reasons, res.reasons
    assert broker.crypto_positions() == {}
    assert broker.claimed_crypto_round_keys() == set()
    print("PASS buying DOWN is aborted when Binance spot price is above strike price")


def test_binance_stoploss_down_allowed_when_spot_below_strike():
    # 41. DOWN trade allowed when Binance spot price is at or below the strike price
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="eth", market_id="1404", up=0.03, down=0.97,
        tokens=("b4-up", "b4-down"),
        question="ETH > $2,450 at 1:30 PM?",
    )
    mock_binance = {"ETH": 2445.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 1, (res, logs)
    positions = list(broker.crypto_positions().values())
    assert len(positions) == 1
    assert positions[0]["outcome_label"] == "Down"
    print("PASS buying DOWN succeeds when Binance spot price is below strike price")


def test_binance_stoploss_toggle_disabled():
    # 42. When crypto_binance_stoploss is False, trade is NOT aborted even if spot violates strike
    configure(crypto_binance_stoploss=False)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1405", up=0.95, down=0.05,
        tokens=("b5-up", "b5-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    mock_binance = {"BTC": 57000.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 1, res
    assert len(broker.crypto_positions()) == 1
    print("PASS trade proceeds when crypto_binance_stoploss is disabled")


def test_binance_stoploss_no_strike_in_question_proceeds():
    # 43. When market question has no strike price (e.g. standard Up/Down), trade proceeds safely
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1406", up=0.95, down=0.05,
        tokens=("b6-up", "b6-down"),
        question="BTC Up or Down",
    )
    mock_binance = {"BTC": 50000.0}
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))
    res = strat.run_once(now=NOW)

    assert res.entries == 1, res
    assert len(broker.crypto_positions()) == 1
    print("PASS markets without explicit strike prices in question proceed normally")


def test_binance_stoploss_api_failure_fallback():
    # 44. When Binance API fails, logs warning and does not crash or abort
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="sol", market_id="1407", up=0.95, down=0.05,
        tokens=("b7-up", "b7-down"),
        question="SOL > $135.50 at 2:00 PM?",
    )
    def failing_binance(asset):
        raise RuntimeError("Binance API 503 Outage")

    strat, logs = strategy(broker, client, binance_client=failing_binance)
    res = strat.run_once(now=NOW)

    assert res.entries == 1, res
    assert len(broker.crypto_positions()) == 1
    print("PASS Binance API failure falls back safely without breaking trade execution")


def test_binance_stoploss_settings_parsing():
    # 45. crypto_settings parses boolean, string, and default crypto_binance_stoploss values
    s1 = crypto_scanner.crypto_settings({"crypto_binance_stoploss": False})
    assert s1["binance_stoploss"] is False
    s2 = crypto_scanner.crypto_settings({"crypto_binance_stoploss": "false"})
    assert s2["binance_stoploss"] is False
    s3 = crypto_scanner.crypto_settings({"crypto_binance_stoploss": "true"})
    assert s3["binance_stoploss"] is True
    s4 = crypto_scanner.crypto_settings({})
    assert s4["binance_stoploss"] is True
    print("PASS crypto_settings parses crypto_binance_stoploss robustly")


def test_binance_stoploss_live_execution_aborted():
    # 46. Live execution is stopped before sending order to exchange when stoploss triggers
    configure(live_trading=True, crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1408", up=0.95, down=0.05,
        tokens=("b8-up", "b8-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    live = FakeLiveBroker(outcome="filled")
    mock_binance = {"BTC": 57900.0}  # violates UP strike 58010.0
    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, live_provider=lambda: live, log=lambda m: None,
        sleep_fn=lambda s: None, binance_client=lambda a: mock_binance.get(a),
    )
    res = strat.run_once(now=NOW)

    assert res.entries == 0, res
    assert len(live.orders) == 0, "No orders should be placed on live exchange when stoploss triggers"
    assert cm.REASON_BINANCE_STOPLOSS in res.reasons
    print("PASS live exchange order placement is aborted when Binance stoploss triggers")


def test_binance_caching_and_asset_case_insensitivity():
    # 47. Binance spot price caching normalizes casing and prevents duplicate calls within 2s
    broker = fresh_broker()
    calls = []

    def mock_fetch(asset):
        calls.append(asset)
        return 65432.10

    strat = crypto_strategy.CryptoStrategy(
        broker, client=None, log=lambda m: None, binance_client=mock_fetch
    )

    # First fetch for lowercase "btc"
    p1 = strat._get_binance_spot_price("btc")
    assert p1 == 65432.10
    assert len(calls) == 1
    assert calls[0] == "BTC"  # Normalized to uppercase

    # Second fetch for uppercase "BTC" within 2 seconds should hit cache
    p2 = strat._get_binance_spot_price("BTC")
    assert p2 == 65432.10
    assert len(calls) == 1, "Cached price must be reused within 2 seconds regardless of case"

    # Spot price <= 0 should be rejected
    strat._binance_price_cache.clear()
    strat._binance_client = lambda a: 0.0
    assert strat._get_binance_spot_price("ETH") is None
    print("PASS Binance spot price caching normalizes casing and avoids duplicate fetches")


def test_binance_stoploss_execution_abort_cleans_claim_and_sets_reason():
    # 48. When spot price moves against position right before order execution,
    # _execute aborts, records the rejection reason, and cleanly releases round claim
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1409", up=0.95, down=0.05,
        tokens=("b9-up", "b9-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )

    call_count = [0]
    def dynamic_binance(asset):
        call_count[0] += 1
        # Pass on first call (during recheck), fail on second call (during _execute)
        if call_count[0] == 1:
            return 58050.0  # Valid for UP
        return 57900.0      # Breaches UP strike!

    strat = crypto_strategy.CryptoStrategy(
        broker, client=client, log=lambda m: None, binance_client=dynamic_binance
    )
    # Clear cache before _execute to simulate spot move
    orig_execute_paper = strat._execute_paper
    def wrapped_execute_paper(opp, price, cfg):
        strat._binance_price_cache.clear()
        return orig_execute_paper(opp, price, cfg)
    strat._execute_paper = wrapped_execute_paper

    res = strat.run_once(now=NOW)

    assert res.entries == 0, res
    assert cm.REASON_BINANCE_STOPLOSS in res.reasons, res.reasons
    assert broker.crypto_positions() == {}, "No positions booked when execution aborted"
    assert broker.claimed_crypto_round_keys() == set(), "Round claim must be released on abort"
    print("PASS stoploss abort in execution cleanly releases claim and records rejection reason")


def test_binance_negative_caching_on_failure():
    # 49. Binance fetch failure is negatively cached so rapid subsequent checks don't repeat stalls
    broker = fresh_broker()
    calls = []

    def failing_binance(asset):
        calls.append(asset)
        return None

    strat = crypto_strategy.CryptoStrategy(
        broker, client=None, log=lambda m: None, binance_client=failing_binance
    )
    p1 = strat._get_binance_spot_price("BTC")
    assert p1 is None
    assert len(calls) == 1

    # Second call within TTL should be served immediately from negative cache
    p2 = strat._get_binance_spot_price("BTC")
    assert p2 is None
    assert len(calls) == 1, "Failed fetch must be cached (negative cache) to avoid repeat network stalls"
    print("PASS Binance failure caching (negative cache) avoids duplicate network stalls")


def test_binance_late_round_timeout_and_fallback_bounding():
    # 50. Late-round execution (< 5s and < 2.5s) bounds Binance timeout and disables secondary fallback
    broker = fresh_broker()
    recorded_calls = []

    class MockClient:
        def get_spot_price(self, asset, timeout=None, allow_fallback=True):
            recorded_calls.append({"asset": asset, "timeout": timeout, "allow_fallback": allow_fallback})
            return 58500.0

    mock_client = MockClient()
    strat = crypto_strategy.CryptoStrategy(
        broker, client=None, log=lambda m: None, binance_client=mock_client
    )

    cfg = {"crypto_binance_stoploss": True}
    # Case A: 10s remaining (normal window) -> default timeout, fallback allowed
    opp_normal = {
        "asset": "BTC",
        "side": "UP",
        "strike_price": 58000.0,
        "seconds_remaining": 10.0,
    }
    ok, _, _ = strat.check_binance_stoploss(opp_normal, cfg)
    assert ok is True
    assert recorded_calls[-1]["timeout"] is None
    assert recorded_calls[-1]["allow_fallback"] is True

    # Case B: 4.0s remaining -> timeout bounded to 1.0s, fallback disabled
    strat._binance_price_cache.clear()
    opp_late = {
        "asset": "BTC",
        "side": "UP",
        "strike_price": 58000.0,
        "seconds_remaining": 4.0,
    }
    ok, _, _ = strat.check_binance_stoploss(opp_late, cfg)
    assert ok is True
    assert recorded_calls[-1]["timeout"] == 1.0
    assert recorded_calls[-1]["allow_fallback"] is False

    # Case C: 2.0s remaining -> timeout bounded to 0.6s, fallback disabled
    strat._binance_price_cache.clear()
    opp_critical = {
        "asset": "BTC",
        "side": "UP",
        "strike_price": 58000.0,
        "seconds_remaining": 2.0,
    }
    ok, _, _ = strat.check_binance_stoploss(opp_critical, cfg)
    assert ok is True
    assert recorded_calls[-1]["timeout"] == 0.6
    assert recorded_calls[-1]["allow_fallback"] is False
    print("PASS late-round execution dynamically bounds Binance timeout and suppresses secondary fallback")


def test_binance_stoploss_dict_opportunity_and_throttled_logging():
    # 51. check_binance_stoploss supports dict representations and throttles unavailable price logs
    broker = fresh_broker()
    logged = []
    strat = crypto_strategy.CryptoStrategy(
        broker, client=None, log=logged.append, binance_client=lambda a: None
    )

    opp_dict = {
        "asset": "BTC",
        "side": "UP",
        "strike_price": 58010.0,
    }
    cfg = {"crypto_binance_stoploss": True}

    # First check: logs warning throttled
    ok1, reason1, detail1 = strat.check_binance_stoploss(opp_dict, cfg)
    assert ok1 is True
    assert detail1 == "binance price unavailable"
    initial_log_count = len([m for m in logged if "Binance spot price unavailable" in m])
    assert initial_log_count == 1

    # Second check immediately after: should NOT spam duplicate log due to throttle
    ok2, reason2, detail2 = strat.check_binance_stoploss(opp_dict, cfg)
    assert ok2 is True
    after_log_count = len([m for m in logged if "Binance spot price unavailable" in m])
    assert after_log_count == 1, "Unavailable price warning must be throttled to prevent log spam"
    print("PASS dictionary opportunity evaluation and Binance error log throttling succeed")


def test_binance_stoploss_dynamic_settings_reload():
    # 52. Toggling crypto_binance_stoploss via settings_manager updates running strategy without restart
    configure(crypto_binance_stoploss=True)
    broker = fresh_broker()
    market, client = build(
        asset="btc", market_id="1410", up=0.95, down=0.05,
        tokens=("b10-up", "b10-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    mock_binance = {"BTC": 57000.0}  # Violates UP strike
    strat, logs = strategy(broker, client, binance_client=lambda a: mock_binance.get(a))

    # Pass 1: Stoploss enabled (default True) -> trade is aborted
    res1 = strat.run_once(now=NOW)
    assert res1.entries == 0
    assert cm.REASON_BINANCE_STOPLOSS in res1.reasons
    assert broker.claimed_crypto_round_keys() == set()

    # Dynamic toggle mid-flight: disable stoploss in settings.json
    settings_manager.update_setting("crypto_binance_stoploss", False)

    # Pass 2: Running strategy picks up change dynamically without restart -> trade enters
    res2 = strat.run_once(now=NOW)
    assert res2.entries == 1
    assert len(broker.crypto_positions()) == 1

    # Dynamic toggle back to True: next round is protected again
    settings_manager.update_setting("crypto_binance_stoploss", True)
    broker2 = fresh_broker()
    market3, client3 = build(
        asset="btc", market_id="1411", up=0.95, down=0.05,
        tokens=("b11-up", "b11-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    strat3, _ = strategy(broker2, client3, binance_client=lambda a: mock_binance.get(a))
    res3 = strat3.run_once(now=NOW)
    assert res3.entries == 0
    assert cm.REASON_BINANCE_STOPLOSS in res3.reasons
    print("PASS toggling crypto_binance_stoploss dynamically reloads in running strategy")


def test_binance_stoploss_taker_and_maker_modes():
    # 53. Binance stop-loss check protects both Taker (crossing book) and Maker order execution
    mock_binance = {"BTC": 57000.0}  # Violates UP strike 58010.0

    # Mode A: Taker Mode (crypto_maker_mode=False)
    configure(crypto_maker_mode=False, crypto_binance_stoploss=True)
    broker_taker = fresh_broker()
    market_taker, client_taker = build(
        asset="btc", market_id="1412", up=0.95, down=0.05,
        tokens=("b12-up", "b12-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    strat_taker, _ = strategy(broker_taker, client_taker, binance_client=lambda a: mock_binance.get(a))
    res_taker = strat_taker.run_once(now=NOW)
    assert res_taker.entries == 0
    assert cm.REASON_BINANCE_STOPLOSS in res_taker.reasons
    assert len(broker_taker.crypto_positions()) == 0

    # Mode B: Maker Mode (crypto_maker_mode=True)
    configure(crypto_maker_mode=True, crypto_binance_stoploss=True)
    broker_maker = fresh_broker()
    market_maker, client_maker = build(
        asset="btc", market_id="1413", up=0.95, down=0.05,
        tokens=("b13-up", "b13-down"),
        question="BTC > $58,010 at 12:00 PM?",
    )
    strat_maker, _ = strategy(broker_maker, client_maker, binance_client=lambda a: mock_binance.get(a))
    res_maker = strat_maker.run_once(now=NOW)
    assert res_maker.entries == 0
    assert cm.REASON_BINANCE_STOPLOSS in res_maker.reasons
    assert len(broker_maker.crypto_positions()) == 0
    print("PASS Binance stoploss aborts trades in both Taker and Maker execution modes")


if __name__ == "__main__":
    test_qualifying_up_round()
    test_repeated_scans_inside_window()
    test_claim_survives_restart()
    test_qualifying_down_round()
    test_different_round_same_market()
    test_entry_windows_boundaries()
    test_recheck_round_expired()
    test_recheck_price_decay()
    test_recheck_slippage()
    test_kill_switch_and_pause()
    test_risk_budget_open_positions()
    test_risk_budget_daily_trade_cap()
    test_disabled_strategy()
    test_skipped_rounds_reasons()
    test_live_execution_fills_and_cancels()
    test_fill_failing_bookkeeping()
    test_maker_order_pricing()
    test_maker_mode_disabled()
    test_maker_tick_size_rounding()
    test_auto_cancel_safety_net()
    test_partial_fills_strategy()
    test_full_fill_during_wait_window()
    test_maker_full_fill_not_in_open_orders()
    test_maker_price_capped_at_max_limit()
    test_maker_fallback_when_no_bid()
    test_exception_safety()
    test_auto_cancel_zero_seconds()
    test_order_matched_during_cancel()
    test_crypto_settings_robust_parsing()
    test_account_session_get_order_shapes()
    test_maker_order_object_shape()
    test_auto_cancel_failure_leaves_order_resting()
    test_maker_price_above_ceiling_rejected()
    test_account_session_get_order_positional_and_open_orders_objects()
    test_negative_cancel_seconds_floored()
    test_binance_symbol_normalization()
    test_binance_client_rest_mock()
    test_strike_price_extraction_various_assets()
    test_binance_stoploss_up_aborted_when_spot_below_strike()
    test_binance_stoploss_up_allowed_when_spot_above_strike()
    test_binance_stoploss_down_aborted_when_spot_above_strike()
    test_binance_stoploss_down_allowed_when_spot_below_strike()
    test_binance_stoploss_toggle_disabled()
    test_binance_stoploss_no_strike_in_question_proceeds()
    test_binance_stoploss_api_failure_fallback()
    test_binance_stoploss_settings_parsing()
    test_binance_stoploss_live_execution_aborted()
    test_binance_caching_and_asset_case_insensitivity()
    test_binance_stoploss_execution_abort_cleans_claim_and_sets_reason()
    test_binance_negative_caching_on_failure()
    test_binance_late_round_timeout_and_fallback_bounding()
    test_binance_stoploss_dict_opportunity_and_throttled_logging()
    test_binance_stoploss_dynamic_settings_reload()
    test_binance_stoploss_taker_and_maker_modes()

    print("\nALL crypto_strategy TESTS PASSED")

