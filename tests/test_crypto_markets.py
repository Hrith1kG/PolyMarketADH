"""Classification and timing rules for the Crypto 5-Minute strategy.

Fixtures mirror what the live Gamma API actually returns for these rounds --
canonical `{asset}-updown-{duration}-{startEpoch}` slugs, a `startDate` that is
the listing time roughly a day before the round, and a null volume. Covers every
approved asset, both entry windows the requirement names, the expiry boundary,
and the refusals that keep the bot out of anything that is not a live 5-minute
Up/Down round.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"] = "state_cm.json"
os.environ["TRADES_DB_FILE"] = "trades_cm.db"
os.environ["SETTINGS_FILE"] = "settings_cm.json"

from datetime import timedelta

import crypto_markets as cm
from tests.crypto_fixtures import make_market, NOW

# 1. Every approved asset classifies from its canonical slug.
for symbol, ticker in [("BTC", "btc"), ("ETH", "eth"), ("SOL", "sol"),
                       ("XRP", "xrp"), ("DOGE", "doge")]:
    round_ = cm.classify_market(make_market(asset=ticker, seconds_left=20))
    assert round_.asset == symbol, (ticker, round_.asset)
    assert round_.duration_seconds == 300.0
    assert round_.duration_source == "slug_schedule"
print("PASS BTC/ETH/SOL/XRP/DOGE all classify from the canonical slug")

# 2. THE REGRESSION THAT MATTERS: state.start_date is the listing time, roughly a
#    day before the round. Measuring the round with it yields ~86,000 seconds and
#    would reject every genuine market -- which is exactly what the first version
#    of this module did against live data. The round length must come from the
#    slug and be cross-checked against the end timestamp instead.
market = make_market(asset="btc", seconds_left=20, listing_offset_seconds=86400)
gap = (market.state.end_date - market.state.start_date).total_seconds()
assert gap > 80000, gap   # the fixture really is serving the misleading value
round_ = cm.classify_market(market)
assert round_.duration_seconds == 300.0, round_.duration_seconds
assert round_.round_start == market.state.end_date - timedelta(seconds=300)
print("PASS a listing timestamp ~24h before the round does not break classification")

# 3. The slug's schedule is cross-checked against the API's end date. A slug that
#    disagrees with the round it is attached to is refused, not guessed at.
bad = make_market(asset="btc", seconds_left=20)
bad.slug = "btc-updown-5m-1000000000"   # a well-formed epoch, but the wrong round
try:
    cm.classify_market(bad)
    raise AssertionError("a slug disagreeing with the end date must be refused")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_WRONG_DURATION, exc.reason
    assert "disagrees" in exc.detail, exc.detail
print("PASS a slug whose schedule contradicts the round end is refused")

# 4. An unapproved coin is refused even though its market is otherwise identical.
#    These trade alongside the approved ones on the live API.
for ticker in ("bnb", "hype", "zec", "ada"):
    try:
        cm.classify_market(make_market(asset=ticker, seconds_left=20))
        raise AssertionError(f"{ticker} must not classify as an approved asset")
    except cm.MarketRejected as exc:
        assert exc.reason == cm.REASON_UNKNOWN_ASSET, (ticker, exc.reason)
print("PASS unapproved coins trading the same product are refused")

# 5. Asset selection narrows the universe without touching the approved table.
try:
    cm.classify_market(make_market(asset="doge", seconds_left=20), allowed_symbols=["BTC", "ETH"])
    raise AssertionError("DOGE must be refused when it is not selected")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_ASSET_NOT_SELECTED, exc.reason
assert cm.selected_symbols(["btc", "Bitcoin", "nonsense"]) == ("BTC",)
assert cm.selected_symbols(None) == ("BTC", "ETH", "SOL", "XRP", "DOGE")
print("PASS the selected-asset list narrows the universe")

# 6. Only exact Up/Down outcome labels count.
try:
    cm.classify_market(make_market(asset="btc", seconds_left=20, labels=("Yes", "No")))
    raise AssertionError("Yes/No outcomes must not classify as an Up/Down round")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_NOT_UP_DOWN, exc.reason
print("PASS Up/Down is decided by outcome labels, not the title")

# 7. Duration: the 15-minute and hourly rounds of the SAME approved asset trade
#    alongside the 5-minute ones and must be refused.
for duration, label in [(900, "15m"), (3600, "1h")]:
    market = make_market(asset="btc", seconds_left=20, duration_seconds=duration)
    assert label in market.slug, market.slug
    try:
        cm.classify_market(market)
        raise AssertionError(f"a {label} round must not qualify as a 5-minute round")
    except cm.MarketRejected as exc:
        assert exc.reason == cm.REASON_WRONG_DURATION, (label, exc.reason)
print("PASS the 15-minute and hourly rounds of an approved asset are refused")

# 8. A non-canonical slug with no duration marker is ambiguous, not assumed --
#    and one that does state its cadence is accepted on the fallback path.
try:
    cm.classify_market(make_market(asset="btc", seconds_left=20,
                                   slug="bitcoin-up-or-down-september-12"))
    raise AssertionError("an unmeasurable round must be refused")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_AMBIGUOUS_DURATION, exc.reason

fallback = cm.classify_market(make_market(asset="btc", seconds_left=20,
                                          slug="bitcoin-up-or-down-5-min-september-12"))
assert fallback.asset == "BTC" and fallback.duration_source == "slug_marker", fallback
print("PASS an unmeasurable round is refused; an explicit 5-minute marker is honoured")

# 9. The entry window, at both configured widths named in the requirement.
for window in (30, 60):
    assert cm.entry_window_check(window - 0.5, window)[0] is True
    assert cm.entry_window_check(window, window)[0] is True, "the first tick of the window is tradable"
    ok, reason, _ = cm.entry_window_check(window + 0.01, window)
    assert ok is False and reason == cm.REASON_TOO_EARLY, (window, reason)
print("PASS 30s and 60s entry windows admit only 0 < remaining <= window")

# 10. The expiry boundary: 0 seconds left is expired, not tradable.
ok, reason, _ = cm.entry_window_check(0.0, 30)
assert ok is False and reason == cm.REASON_EXPIRED, reason
ok, reason, _ = cm.entry_window_check(-0.001, 30)
assert ok is False and reason == cm.REASON_EXPIRED, reason
assert cm.entry_window_check(0.001, 30)[0] is True, "a round still open is tradable"
assert cm.entry_window_check(float("nan"), 30)[0] is False
assert cm.entry_window_check(float("inf"), 30)[0] is False
print("PASS expiry boundary: 0s and past are refused, an open round is not")

# 11. Round keys are per market AND per round.
m1 = cm.classify_market(make_market(asset="btc", seconds_left=20, market_id="777"))
m2 = cm.classify_market(make_market(asset="btc", seconds_left=320, market_id="777"))
assert m1.round_key != m2.round_key, "two rounds of one market must not share a key"
m1b = cm.classify_market(make_market(asset="btc", seconds_left=20, market_id="777"))
assert m1.round_key == m1b.round_key, "the same round must produce a stable key"
print("PASS round keys are stable per round and distinct across rounds")

# 12. Liveness is refused for anything that cannot accept an order.
for kwargs, expect_live in [
    ({}, True),
    ({"closed": True}, False),
    ({"accepting_orders": False}, False),
    ({"active": False}, False),
    ({"archived": True}, False),
]:
    assert cm.is_market_live(make_market(asset="btc", seconds_left=20, **kwargs))[0] is expect_live, kwargs
print("PASS closed / archived / inactive / order-refusing markets are not live")

# 13. Gamma reports no volume on a round this young; that must not be read as a
#     zero that trips a floor comparison.
round_ = cm.classify_market(make_market(asset="btc", seconds_left=20, volume=None))
assert round_.volume == 0.0 and round_.liquidity == 2000.0, (round_.volume, round_.liquidity)
print("PASS a null volume is read as 0.0 rather than crashing the classifier")

# 14. Timezone-naive timestamps are read as UTC rather than local time.
naive = cm.classify_market(make_market(asset="btc", seconds_left=25, naive_timestamps=True))
assert 24.0 <= naive.seconds_remaining(NOW) <= 26.0, naive.seconds_remaining(NOW)
print("PASS naive timestamps are interpreted as UTC")

print("\nALL crypto_markets TESTS PASSED")
