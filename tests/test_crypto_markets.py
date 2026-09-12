"""Classification and timing rules for the Crypto 5-Minute strategy.

Covers every approved asset, both entry windows the requirement names, the
expiry boundary, and the refusals that keep the bot out of anything that is not
a live 5-minute Up/Down round.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())
os.environ["STATE_FILE"] = "state_cm.json"
os.environ["TRADES_DB_FILE"] = "trades_cm.db"
os.environ["SETTINGS_FILE"] = "settings_cm.json"

import crypto_markets as cm
from tests.crypto_fixtures import make_market, NOW


# 1. Every approved asset is recognised from its slug, and nothing else is.
for symbol, slug in [
    ("BTC", "bitcoin-up-or-down-2026-09-12-14-05"),
    ("ETH", "ethereum-up-or-down-2026-09-12-14-05"),
    ("SOL", "solana-up-or-down-2026-09-12-14-05"),
    ("XRP", "xrp-up-or-down-2026-09-12-14-05"),
    ("DOGE", "dogecoin-up-or-down-2026-09-12-14-05"),
]:
    market = make_market(slug=slug, seconds_left=20)
    round_ = cm.classify_market(market)
    assert round_.asset == symbol, (slug, round_.asset)
print("PASS BTC/ETH/SOL/XRP/DOGE all classify from their slug")

# Ticker-style slugs resolve to the same canonical symbols.
for symbol, slug in [
    ("BTC", "btc-up-or-down-5m-2026-09-12-1405"),
    ("ETH", "eth-up-or-down-5m-2026-09-12-1405"),
    ("SOL", "sol-up-or-down-5m-2026-09-12-1405"),
    ("XRP", "xrp-up-or-down-5m-2026-09-12-1405"),
    ("DOGE", "doge-up-or-down-5m-2026-09-12-1405"),
]:
    assert cm.classify_market(make_market(slug=slug, seconds_left=20)).asset == symbol
print("PASS ticker-style slugs resolve to the same symbols")

# 2. An unapproved coin is refused, however well formed the market is.
try:
    cm.classify_market(make_market(slug="cardano-up-or-down-2026-09-12-14-05", seconds_left=20))
    raise AssertionError("ADA must not classify as an approved asset")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_UNKNOWN_ASSET, exc.reason
print("PASS an unapproved coin is refused")

# 3. Asset selection narrows the universe without touching the approved table.
try:
    cm.classify_market(
        make_market(slug="dogecoin-up-or-down-2026-09-12-14-05", seconds_left=20),
        allowed_symbols=["BTC", "ETH"],
    )
    raise AssertionError("DOGE must be refused when it is not selected")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_ASSET_NOT_SELECTED, exc.reason
assert cm.selected_symbols(["btc", "Bitcoin", "nonsense"]) == ("BTC",)
assert cm.selected_symbols(None) == ("BTC", "ETH", "SOL", "XRP", "DOGE")
print("PASS the selected-asset list narrows the universe")

# 4. Only exact Up/Down outcome labels count -- a Yes/No market of the same name
#    is not one of these rounds.
try:
    cm.classify_market(make_market(
        slug="bitcoin-up-or-down-2026-09-12-14-05", seconds_left=20, labels=("Yes", "No")))
    raise AssertionError("Yes/No outcomes must not classify as an Up/Down round")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_NOT_UP_DOWN, exc.reason
print("PASS Up/Down is decided by outcome labels, not the title")

# 5. Duration: only a five-minute round qualifies. An hourly "up or down" market
#    for the same coin is refused even though its slug looks identical.
try:
    cm.classify_market(make_market(
        slug="bitcoin-up-or-down-2026-09-12-14-00", seconds_left=20, duration_seconds=3600))
    raise AssertionError("an hourly round must not qualify as a 5-minute round")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_WRONG_DURATION, exc.reason

# ... and a round with no measurable duration at all is ambiguous, not assumed.
try:
    cm.classify_market(make_market(
        slug="bitcoin-up-or-down-2026-09-12-14-05", seconds_left=20, start=None))
    raise AssertionError("an unmeasurable round must be refused")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_AMBIGUOUS_DURATION, exc.reason

# ... unless the slug itself states the cadence.
round_ = cm.classify_market(make_market(
    slug="btc-up-or-down-5m-2026-09-12-1405", seconds_left=20, start=None))
assert round_.duration_source == "slug", round_.duration_source
assert cm.classify_market(make_market(slug="bitcoin-up-or-down-x", seconds_left=20)).duration_source == "timestamps"
print("PASS only verified 5-minute rounds qualify; 60-minute and unmeasurable ones do not")

# 6. The entry window, at both configured widths named in the requirement.
for window in (30, 60):
    assert cm.entry_window_check(window - 0.5, window)[0] is True
    assert cm.entry_window_check(window, window)[0] is True, "the first tick of the window is tradable"
    ok, reason, _ = cm.entry_window_check(window + 0.01, window)
    assert ok is False and reason == cm.REASON_TOO_EARLY, (window, reason)
print("PASS 30s and 60s entry windows admit only 0 < remaining <= window")

# 7. The expiry boundary: 0 seconds left is expired, not tradable.
ok, reason, _ = cm.entry_window_check(0.0, 30)
assert ok is False and reason == cm.REASON_EXPIRED, reason
ok, reason, _ = cm.entry_window_check(-0.001, 30)
assert ok is False and reason == cm.REASON_EXPIRED, reason
assert cm.entry_window_check(0.001, 30)[0] is True, "a round still open is tradable"
assert cm.entry_window_check(float("nan"), 30)[0] is False
assert cm.entry_window_check(float("inf"), 30)[0] is False
print("PASS expiry boundary: 0s and past are refused, an open round is not")

# 8. Round keys are per market AND per round, so a claim cannot leak across rounds.
m1 = cm.classify_market(make_market(slug="bitcoin-up-or-down-a", seconds_left=20, market_id="777"))
m2 = cm.classify_market(make_market(slug="bitcoin-up-or-down-b", seconds_left=320, market_id="777"))
assert m1.round_key != m2.round_key, "two rounds of one market must not share a key"
m1b = cm.classify_market(make_market(slug="bitcoin-up-or-down-a", seconds_left=20, market_id="777"))
assert m1.round_key == m1b.round_key, "the same round must produce a stable key"
print("PASS round keys are stable per round and distinct across rounds")

# 9. Liveness is refused for anything that cannot accept an order.
for kwargs, expect_live in [
    ({}, True),
    ({"closed": True}, False),
    ({"accepting_orders": False}, False),
    ({"active": False}, False),
    ({"archived": True}, False),
]:
    market = make_market(slug="bitcoin-up-or-down-a", seconds_left=20, **kwargs)
    assert cm.is_market_live(market)[0] is expect_live, kwargs
print("PASS closed / archived / inactive / order-refusing markets are not live")

# 10. A market naming two approved coins is ambiguous, not guessed at.
try:
    cm.classify_market(make_market(slug="bitcoin-vs-ethereum-up-or-down-a", seconds_left=20))
    raise AssertionError("a two-asset slug must be refused")
except cm.MarketRejected as exc:
    assert exc.reason == cm.REASON_AMBIGUOUS_ASSET, exc.reason
print("PASS a round naming two approved assets is refused as ambiguous")

# 11. Timezone-naive end timestamps are read as UTC rather than local time.
naive = make_market(slug="bitcoin-up-or-down-a", seconds_left=25, naive_timestamps=True)
round_naive = cm.classify_market(naive)
assert 24.0 <= round_naive.seconds_remaining(NOW) <= 26.0, round_naive.seconds_remaining(NOW)
print("PASS naive timestamps are interpreted as UTC")

print("\nALL crypto_markets TESTS PASSED")
