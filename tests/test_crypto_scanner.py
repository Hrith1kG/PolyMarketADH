"""Candidate discovery and order-book qualification for the Crypto 5-Minute strategy."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"] = "state_cs.json"
os.environ["TRADES_DB_FILE"] = "trades_cs.db"
os.environ["SETTINGS_FILE"] = "settings_cs.json"

from datetime import timedelta

import crypto_markets as cm
import crypto_scanner
import settings_manager
from tests.crypto_fixtures import FakeBook, FakeClient, NOW, make_market


def settings(**overrides):
    base = dict(settings_manager.DEFAULT_SETTINGS)
    base["crypto_enabled"] = True
    base.update(overrides)
    return base


def scan(markets, books, **overrides):
    client = FakeClient(markets=markets, books=books)
    result = crypto_scanner.find_crypto_opportunities(
        client=client, settings_override=settings(**overrides), now=NOW
    )
    return client, result


def reasons(result):
    return {s.reason for s in result.skipped}


# 1. Each approved asset produces a signal on the side that clears the floor.
#    Up and Down are judged independently: only the qualifying side comes back.
for asset, ticker, up_price, down_price, expect_side in [
    ("BTC", "btc", 0.96, 0.04, "UP"),
    ("ETH", "eth", 0.02, 0.98, "DOWN"),
    ("SOL", "sol", 0.93, 0.07, "UP"),
    ("XRP", "xrp", 0.05, 0.95, "DOWN"),
    ("DOGE", "doge", 0.91, 0.09, "UP"),
]:
    market = make_market(asset=ticker, seconds_left=20, prices=(up_price, down_price),
                         tokens=(f"{asset}-up", f"{asset}-down"))
    books = {
        f"{asset}-up": FakeBook(best_bid=up_price - 0.01, best_ask=up_price),
        f"{asset}-down": FakeBook(best_bid=down_price - 0.01, best_ask=down_price),
    }
    _client, result = scan([market], books)
    assert len(result.opportunities) == 1, (asset, result.opportunities)
    opp = result.opportunities[0]
    assert opp.asset == asset and opp.side == expect_side, (opp.asset, opp.side)
    assert opp.outcome_label == expect_side.capitalize()
    assert opp.market_type == crypto_scanner.CRYPTO_MARKET_TYPE
    assert abs(opp.confirmed_price - (up_price if expect_side == "UP" else down_price)) < 1e-9
    # The side that failed the floor is recorded with a reason, not silently dropped.
    assert cm.REASON_PRICE_BELOW_THRESHOLD in reasons(result), reasons(result)
print("PASS BTC/ETH/SOL/XRP/DOGE each signal on the qualifying side only (Up and Down)")

# 2. The threshold is applied to the executable ask, not the cached quote. A
#    market quoted at 0.95 whose real ask is 0.88 must NOT trade.
market = make_market(asset="btc", seconds_left=15, prices=(0.95, 0.05))
_client, result = scan([market], {
    "tok-up": FakeBook(best_bid=0.86, best_ask=0.88),
    "tok-down": FakeBook(best_bid=0.04, best_ask=0.05),
})
assert result.opportunities == [], result.opportunities
assert cm.REASON_PRICE_BELOW_THRESHOLD in reasons(result)
print("PASS the floor is enforced on the executable ask, not the cached quote")

# 3. The 30s and 60s windows gate discovery exactly.
for window in (30, 60):
    inside = make_market(asset="btc", seconds_left=window - 1, market_id="10")
    edge = make_market(asset="eth", seconds_left=window, market_id="11")
    outside = make_market(asset="sol", seconds_left=window + 5, market_id="12")
    books = {"tok-up": FakeBook(best_bid=0.92, best_ask=0.94),
             "tok-down": FakeBook(best_bid=0.05, best_ask=0.06)}
    _client, result = scan([inside, edge, outside], books, crypto_entry_window_seconds=window)
    got = sorted(o.market_id for o in result.opportunities)
    assert got == ["10", "11"], (window, got)
    assert cm.REASON_TOO_EARLY in reasons(result), reasons(result)
print("PASS 30s and 60s windows admit only rounds inside them")

# 4. An expired round is never traded, even while it is still listed as open.
expired = make_market(asset="btc", seconds_left=0, market_id="20")
_client, result = scan([expired], {"tok-up": FakeBook(best_ask=0.99)})
assert result.opportunities == []
assert cm.REASON_EXPIRED in reasons(result), reasons(result)
print("PASS a round at its expiry boundary is refused")

# 5. Markets that are not live are refused before any book is fetched.
for kwargs, _label in [({"closed": True}, "closed"), ({"accepting_orders": False}, "no orders")]:
    market = make_market(asset="btc", seconds_left=20, **kwargs)
    client, result = scan([market], {"tok-up": FakeBook(best_ask=0.99)})
    assert result.opportunities == []
    assert cm.REASON_NOT_LIVE in reasons(result), reasons(result)
    assert client.book_calls == [], "a dead market must not cost an order book request"
print("PASS non-live markets are refused without touching the order book")

# 6. Order-book health: one-sided, wide, stale and thin books are all refused.
base = make_market(asset="btc", seconds_left=20, prices=(0.95, 0.05))
for book, expected in [
    (FakeBook(one_sided="no_asks"), cm.REASON_BOOK_NO_ASKS),
    (FakeBook(one_sided="no_bids"), cm.REASON_BOOK_NO_BIDS),
    (FakeBook(best_bid=0.50, best_ask=0.95), cm.REASON_BOOK_WIDE_SPREAD),
    (FakeBook(best_bid=0.94, best_ask=0.95, timestamp=NOW - timedelta(seconds=120)), cm.REASON_BOOK_STALE),
    (FakeBook(best_bid=0.94, best_ask=0.95, ask_size=1.0), cm.REASON_BOOK_THIN),
]:
    _client, result = scan([base], {"tok-up": book, "tok-down": FakeBook(best_ask=0.05)})
    assert result.opportunities == [], (expected, result.opportunities)
    assert expected in reasons(result), (expected, reasons(result))
# A healthy book at the same price does qualify, proving the refusals above are
# the book's doing and not the price's.
_client, result = scan([base], {"tok-up": FakeBook(best_bid=0.94, best_ask=0.95),
                                "tok-down": FakeBook(best_ask=0.05)})
assert len(result.opportunities) == 1, result.skipped
print("PASS no-asks / no-bids / wide / stale / thin books are refused, a healthy one is not")

# 6b. An offer with no bid is refused by default but IS executable, so it can be
#     accepted deliberately -- near the end of a round the favourite's book
#     routinely goes offer-only, which is when this strategy wants to trade.
offer_only = {"tok-up": FakeBook(one_sided="no_bids", best_ask=0.95),
              "tok-down": FakeBook(best_ask=0.05)}
_client, result = scan([base], offer_only)
assert result.opportunities == [] and cm.REASON_BOOK_NO_BIDS in reasons(result), reasons(result)
_client, result = scan([base], offer_only, crypto_require_two_sided_book=False)
assert len(result.opportunities) == 1, (result.opportunities, reasons(result))
assert result.opportunities[0].confirmed_price == 0.95
assert result.opportunities[0].best_bid is None
# An empty ASK side is never tradable, two-sidedness setting or not: there is
# nothing being offered to buy.
_client, result = scan([base], {"tok-up": FakeBook(one_sided="no_asks"), "tok-down": FakeBook(best_ask=0.05)},
                       crypto_require_two_sided_book=False)
assert result.opportunities == [] and cm.REASON_BOOK_NO_ASKS in reasons(result), reasons(result)
print("PASS an offer with no bid is refused by default and tradable when allowed; no offer never is")

# 7. The entry ceiling keeps the bot out of fills with no profit left in them.
_client, result = scan([base], {"tok-up": FakeBook(best_bid=0.999, best_ask=1.0),
                                "tok-down": FakeBook(best_ask=0.05)},
                       crypto_max_probability=0.999)
assert result.opportunities == []
assert cm.REASON_PRICE_ABOVE_CEILING in reasons(result), reasons(result)
print("PASS an ask above the ceiling is refused")

# 8. Wrong shapes never reach the book: hourly rounds, Yes/No markets and
#    unapproved coins are all dropped during classification.
# Exactly what the live API serves alongside the 5-minute rounds: longer
# durations of the same coins, unapproved coins, and unrelated markets.
noise = [
    make_market(asset="btc", seconds_left=20, duration_seconds=900, market_id="30"),
    make_market(asset="btc", seconds_left=20, duration_seconds=3600, market_id="31"),
    make_market(asset="bnb", seconds_left=20, market_id="32"),
    make_market(asset="hype", seconds_left=20, market_id="33"),
    make_market(asset="zec", seconds_left=20, market_id="34"),
    make_market(asset="btc", seconds_left=20, labels=("Yes", "No"), market_id="35"),
    make_market(slug="chelsea-vs-arsenal-moneyline", seconds_left=20,
                labels=("Chelsea", "Arsenal"), market_id="36"),
]
client, result = scan(noise, {"tok-up": FakeBook(best_ask=0.99)})
assert result.opportunities == [] and result.rounds_seen == 0, result.rounds_seen
assert client.book_calls == [], client.book_calls
assert {cm.REASON_WRONG_DURATION, cm.REASON_NOT_UP_DOWN, cm.REASON_UNKNOWN_ASSET} <= reasons(result), reasons(result)
print("PASS longer durations / Yes-No / unapproved coins / sports are dropped before any book call")

# 9. A round already claimed, or a token already held, is skipped.
market = make_market(asset="btc", seconds_left=20, market_id="40")
round_ = cm.classify_market(market)
client = FakeClient([market], {"tok-up": FakeBook(best_bid=0.94, best_ask=0.95)})
result = crypto_scanner.find_crypto_opportunities(
    client=client, settings_override=settings(), now=NOW,
    claimed_round_keys={round_.round_key},
)
assert result.opportunities == [] and cm.REASON_ALREADY_TRADED in reasons(result)
assert client.book_calls == [], "an already-traded round must not cost a book request"

result = crypto_scanner.find_crypto_opportunities(
    client=FakeClient([market], {"tok-up": FakeBook(best_bid=0.94, best_ask=0.95),
                                 "tok-down": FakeBook(best_ask=0.05)}),
    settings_override=settings(), now=NOW, held_token_ids={"tok-up"},
)
assert result.opportunities == [] and cm.REASON_DUPLICATE_POSITION in reasons(result), reasons(result)
print("PASS an already-claimed round and an already-held token are both skipped")

# 10. Discovery is bounded to rounds about to resolve, which is what makes a
#     seconds-level cadence affordable.
client, _result = scan([], {}, crypto_entry_window_seconds=30, crypto_discovery_lookahead_seconds=420)
query = client.list_calls[0]
assert query["closed"] is False and query["end_date_min"] == NOW
assert query["end_date_max"] == NOW + timedelta(seconds=450), query["end_date_max"]
assert "tag_id" not in query, "discovery must not depend on a guessed tag id by default"
client, _result = scan([], {}, crypto_discovery_tag_id=1234)
assert client.list_calls[0]["tag_id"] == 1234
print("PASS discovery is bounded by resolution time and narrows on an optional tag id")

# 11. An unselected asset never signals, even with a perfect book.
market = make_market(asset="doge", seconds_left=20, market_id="50")
_client, result = scan([market], {"tok-up": FakeBook(best_bid=0.94, best_ask=0.95)},
                       crypto_assets=["BTC", "ETH"])
assert result.opportunities == [] and cm.REASON_ASSET_NOT_SELECTED in reasons(result)
print("PASS an unselected asset never produces a signal")

# 12. Signals are ordered by urgency so the closest round is acted on first.
urgent = make_market(asset="btc", seconds_left=5, market_id="60", tokens=("u-up", "u-down"))
later = make_market(asset="eth", seconds_left=25, market_id="61", tokens=("l-up", "l-down"))
_client, result = scan([later, urgent], {
    "u-up": FakeBook(best_bid=0.94, best_ask=0.95), "u-down": FakeBook(best_ask=0.05),
    "l-up": FakeBook(best_bid=0.94, best_ask=0.95), "l-down": FakeBook(best_ask=0.05),
})
assert [o.market_id for o in result.opportunities] == ["60", "61"], [o.market_id for o in result.opportunities]
print("PASS signals are ordered most-urgent-first")

# 13. A failing scan is reported rather than swallowed as "nothing found".
class Exploding(FakeClient):
    def list_markets(self, **kwargs):
        raise RuntimeError("gamma is down")

result = crypto_scanner.find_crypto_opportunities(
    client=Exploding(), settings_override=settings(), now=NOW)
assert result.error and "gamma is down" in result.error, result.error
assert crypto_scanner.LAST_SCAN_ERROR == result.error
print("PASS a broken scan reports an error instead of an empty result")

print("\nALL crypto_scanner TESTS PASSED")
