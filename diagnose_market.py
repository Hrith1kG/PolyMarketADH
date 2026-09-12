"""Explains why a given Polymarket market did or did not qualify as a signal.

Replays every gate in scanner.find_opportunities() in the same order, against your
live settings.json, and prints PASS/FAIL with the actual value next to the threshold.

Usage:
    python diagnose_market.py atp-martine-aboian-2026-09-11
    python diagnose_market.py https://polymarket.com/sports/atp/atp-martine-aboian-2026-09-11
"""
import sys
from datetime import datetime, timezone, timedelta

from polymarket import RateLimitError, PolymarketError
import polymarket_client
import settings_manager
import scanner
import live_timing

OK, NO, INFO = "PASS", "FAIL", "  ->"
_verdicts = []


def check(label, passed, detail):
    _verdicts.append((label, passed))
    print(f"  [{OK if passed else NO}] {label:<34} {detail}")
    return passed


def fmt_dt(dt):
    if dt is None:
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = dt - now
    hrs = delta.total_seconds() / 3600.0
    rel = f"{hrs:+.2f}h ({delta.days:+d}d)" if abs(hrs) >= 1 else f"{delta.total_seconds():+.0f}s"
    return f"{dt.isoformat()}  [{rel} from now]"


def slug_from_arg(arg):
    arg = arg.strip().rstrip("/")
    if "polymarket.com" in arg:
        return arg.split("/")[-1].split("?")[0]
    return arg


def _markets_of(ev):
    return list(getattr(ev, "markets", None) or [])


def resolve_markets(client, slug):
    """The URL may name an event or a single market, and a resolved match is CLOSED.

    Gamma excludes closed records by default (list_events has closed=False baked into
    its signature), so every lookup here either targets closed records explicitly or
    uses a by-id/by-url endpoint that ignores the closed flag entirely.
    """
    # NOTE: the SDK's url= lookup only accepts two-segment /event/<slug> or
    # /market/<slug> paths, so a /sports/<league>/<slug> URL is rejected outright.
    # It resolves to the same endpoint as slug= anyway, so we only use slug=.
    attempts = [
        ("get_event(slug=...)", lambda: _markets_of(client.get_event(slug=slug))),
        ("get_market(slug=...)", lambda: [client.get_market(slug=slug)]),
        ("list_events(slug=..., closed=True)",
         lambda: [m for ev in (client.list_events(slug=slug, closed=True, page_size=20)
                               .first_page().items or []) for m in _markets_of(ev)]),
        ("list_markets(slug=...)",
         lambda: list(client.list_markets(slug=slug, page_size=100).first_page().items or [])),
    ]

    for label, fn in attempts:
        try:
            found = [m for m in (fn() or []) if m is not None]
        except Exception as exc:
            print(f"{INFO} {label:<36} -> {type(exc).__name__}: {exc}")
            continue
        if found:
            print(f"{INFO} {label:<36} -> matched {len(found)} market(s)")
            return found
        print(f"{INFO} {label:<36} -> no match")

    # Last resort: free-text search, explicitly keeping closed markets.
    terms = " ".join(p for p in slug.split("-") if not p.isdigit() and len(p) > 2)
    print(f"{INFO} falling back to search(q={terms!r}, keep_closed_markets=1)")
    try:
        page = client.search(q=terms, keep_closed_markets=1, page_size=10).first_page()
        for res in (page.items or []):
            for ev in (getattr(res, "events", None) or []):
                ms = _markets_of(ev)
                if ms:
                    print(f"{INFO} search matched event '{getattr(ev, 'title', '?')}' "
                          f"(slug={getattr(ev, 'slug', '?')}) with {len(ms)} market(s)")
                    return ms
            for m in (getattr(res, "markets", None) or []):
                print(f"{INFO} search matched market '{getattr(m, 'question', '?')}' "
                      f"(slug={getattr(m, 'slug', '?')})")
                return [m]
    except Exception as exc:
        print(f"{INFO} search failed: {type(exc).__name__}: {exc}")
    return []



def diagnose_live_timing(market, s):
    """Explains the Late Game timing verdict for the event this market belongs to.

    The in-play fields (period / elapsed / score) live on the Event, not the Market,
    so this resolves the parent event through the live-event feed before judging.
    """
    print("\n-- LIVE GAME TIMING (Late Game) --")
    client = polymarket_client.get_public_client()
    event = None
    try:
        for page in client.list_events(live=True, closed=False, page_size=100):
            for candidate in (page.items or []):
                if any(str(m.id) == str(market.id) for m in (candidate.markets or [])):
                    event = candidate
                    break
            if event:
                break
    except Exception as exc:
        print(f"{INFO} could not read the live-event feed: {type(exc).__name__}: {exc}")
        return

    if event is None:
        print(f"  [{NO}] this market's event is not in the live-event feed, so Late Game "
              f"never sees it.\n       Either the match is not underway, or Polymarket "
              f"does not mark it live.")
        return

    sp = event.sports
    print(f"{INFO} event: {event.title!r}")
    print(f"{INFO} sport={live_timing.sport_code(event)!r} period={sp.period!r} "
          f"elapsed={sp.elapsed!r} score={sp.score!r}")
    print(f"{INFO} state.live={event.state.live!r} state.ended={event.state.ended!r}")

    decision = live_timing.evaluate_event_timing(event, s)
    check("late game timing", decision.eligible, decision.describe())
    if not decision.eligible and decision.reason == live_timing.REASON_SPORT_UNSUPPORTED:
        print(f"{INFO} add a rule to live_timing.SPORT_RULES (or a family tag) to "
              f"support this sport.")


def diagnose(market, s):
    print("=" * 78)
    print(f"MARKET: {market.question or market.slug}")
    print(f"  id={market.id}  slug={market.slug}")

    st = market.state
    sp = market.sports
    me = market.metrics

    if st and st.closed:
        print("\n  !! THIS MARKET IS ALREADY CLOSED/RESOLVED. Volume, liquidity, prices\n"
              "     and the order book below are TODAY's values, not what the scanner saw\n"
              "     at the time of the miss. The TIMING block is still exact -- end_date\n"
              "     and game_start_time do not change after resolution.")

    # ---- The answer to "when did it resolve" ----
    print("\n-- TIMING (resolution window) --")
    end_dt = st.end_date if st else None
    start_dt = sp.game_start_time if sp else None
    print(f"{INFO} end_date (RESOLUTION TIME): {fmt_dt(end_dt)}")
    print(f"{INFO} game_start_time:            {fmt_dt(start_dt)}")

    late = bool(s.get("late_game_enabled", False))
    min_hours = float(s.get("min_hours_to_resolution", 1.0))
    max_days = float(s.get("max_days_to_resolution", 30.0))

    if st:
        check("state.closed is False", not st.closed, f"closed={st.closed}")
        check("accepting_orders", st.accepting_orders is not False,
              f"accepting_orders={st.accepting_orders}")
        if late:
            # The resolution window is not applied at all on the Late Game path:
            # end_date means a different thing in every sport (start_time for soccer,
            # start+7d for tennis), so it decides nothing about entry timing.
            print(f"{INFO} late_game is ON: the end_date resolution window is not "
                  f"applied.\n       Timing comes from live in-play state instead -- "
                  f"see the LIVE GAME TIMING block below.")
        else:
            in_win = scanner._within_resolution_window(end_dt, min_hours, max_days)
            hrs = ((end_dt.replace(tzinfo=timezone.utc) if end_dt and end_dt.tzinfo is None else end_dt)
                   - datetime.now(timezone.utc)).total_seconds() / 3600.0 if end_dt else None
            check("resolution window", in_win,
                  f"{hrs:+.2f}h to resolve; need >= {min_hours}h and <= {max_days}d")

    if late:
        diagnose_live_timing(market, s)

    # ---- Category filters ----
    print("\n-- CATEGORY / TYPE --")
    # MarketSportsMetadata exposes sports_market_type (singular); the plural spelling
    # is the *settings* key naming the list of accepted types.
    mtype = sp.sports_market_type if sp else None
    print(f"{INFO} market.sports = {sp!r}")
    print(f"{INFO} sports_market_type on market = {mtype!r}")
    if s.get("only_sports", True):
        want = s.get("sports_market_types", ["moneyline"])
        print(f"{INFO} scanner queries tag_id={s.get('sports_tag_id')} "
              f"sports_market_types={want}")
        print(f"{INFO} NOTE: these are server-side query filters. If this market does not "
              f"carry\n       that tag/type, the scanner never sees it at all.")

    # ---- Metrics ----
    print("\n-- LIQUIDITY / VOLUME --")
    vol = float(me.volume or 0.0) if me else 0.0
    liq = float(me.liquidity or 0.0) if me else 0.0
    check("volume >= min_volume", vol >= float(s.get("min_volume", 5000.0)),
          f"volume={vol:,.2f} vs min={float(s.get('min_volume', 5000.0)):,.2f}")
    check("liquidity >= min_liquidity", liq >= float(s.get("min_liquidity", 1000.0)),
          f"liquidity={liq:,.2f} vs min={float(s.get('min_liquidity', 1000.0)):,.2f}")

    # ---- Per-outcome ----
    pmin = float(s.get("price_min", 0.97))
    pmax = float(s.get("price_max", 0.995))
    stake = float(s.get("stake_per_trade", 25.0) or 0.0)
    healthy = bool(s.get("require_healthy_data", True))
    hiconf = bool(s.get("require_high_confidence", False))
    client = polymarket_client.get_public_client()

    if not market.outcomes:
        print("\n-- OUTCOMES --\n  [FAIL] market.outcomes is empty")
        return

    for outcome in [market.outcomes.yes, market.outcomes.no]:
        if not outcome or not outcome.token_id:
            continue
        print(f"\n-- OUTCOME: {outcome.label}  (token {str(outcome.token_id)[:18]}...) --")
        if outcome.price is None:
            check("gamma price present", False, "outcome.price is None")
            continue
        gp = float(outcome.price)
        if not check("gamma price in band", pmin <= gp <= pmax,
                     f"gamma_price={gp:.4f} vs band [{pmin}, {pmax}]"):
            continue

        if healthy or hiconf:
            try:
                ob = client.get_order_book(token_id=str(outcome.token_id))
            except (RateLimitError, PolymarketError) as exc:
                check("order book fetch", False, f"{type(exc).__name__}: {exc}")
                continue
            if not ob.bids or not ob.asks:
                check("two-sided book", False,
                      f"bids={len(ob.bids)} asks={len(ob.asks)}")
                continue
            bb = float(ob.bids[-1].price)   # bids ascending -> best last
            ba = float(ob.asks[-1].price)   # asks descending -> best last
            spread = ba - bb
            max_spread = (scanner.CONFIDENCE_MAX_SPREAD if hiconf
                          else scanner.HEALTH_MAX_SPREAD)
            max_age = (scanner.CONFIDENCE_MAX_QUOTE_AGE_SECONDS if hiconf
                       else scanner.HEALTH_MAX_QUOTE_AGE_SECONDS)
            print(f"{INFO} best_bid={bb:.4f}  best_ask={ba:.4f}  "
                  f"ask_size={float(ob.asks[-1].size or 0):.2f}  "
                  f"tick={ob.tick_size}  min_order_size={ob.min_order_size}")
            check("spread <= max", spread <= max_spread,
                  f"spread={spread:.4f} vs max={max_spread} "
                  f"({'high-confidence' if hiconf else 'healthy-data'} mode)")
            if ob.timestamp is not None:
                ts = ob.timestamp if ob.timestamp.tzinfo else ob.timestamp.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                check("quote age <= max", age <= max_age,
                      f"age={age:.0f}s vs max={max_age}s  (book ts {ts.isoformat()})")
            else:
                print(f"{INFO} order book has no timestamp -> staleness check skipped")
            if hiconf and stake > 0:
                need = stake / gp if gp > 0 else 0.0
                asz = float(ob.asks[-1].size or 0.0)
                check("ask depth >= stake", asz >= need * scanner.CONFIDENCE_DEPTH_MULTIPLE,
                      f"ask_size={asz:.2f} vs need={need:.2f} shares for ${stake}")
            confirmed = ba
        else:
            try:
                d = client.get_price(token_id=str(outcome.token_id), side="BUY")
            except (RateLimitError, PolymarketError) as exc:
                check("get_price", False, f"{type(exc).__name__}: {exc}")
                continue
            confirmed = float(d) if d is not None else None
            if confirmed is None:
                check("get_price non-null", False, "returned None")
                continue

        check("confirmed price in band", pmin <= confirmed <= pmax,
              f"confirmed={confirmed:.4f} vs band [{pmin}, {pmax}]")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    slug = slug_from_arg(sys.argv[1])
    s = settings_manager.load_settings()

    print(f"SLUG: {slug}")
    print(f"NOW:  {datetime.now(timezone.utc).isoformat()}")
    print("\n-- ACTIVE SETTINGS THAT GATE SIGNALS --")
    for k in ("price_min", "price_max", "min_volume", "min_liquidity",
              "min_hours_to_resolution", "max_days_to_resolution",
              "max_signals_per_scan", "stake_per_trade", "only_sports",
              "sports_market_types", "sports_tag_id", "require_healthy_data",
              "require_high_confidence", "late_game_enabled",
              "late_game_max_remaining_minutes", "late_game_max_remaining_fraction",
              "late_game_min_probability", "late_game_max_probability",
              "late_game_allow_worst_case_periods", "late_game_require_clock",
              "late_game_sport_rules"):
        print(f"  {k:<30} = {s.get(k)!r}")

    client = polymarket_client.get_public_client()
    markets = resolve_markets(client, slug)
    if not markets:
        print("\nNo market found for that slug. It may be closed/archived, or the slug "
              "is an event slug whose markets are not exposed via list_events.")
        sys.exit(1)

    for m in markets:
        diagnose(m, s)

    print("\n" + "=" * 78)
    fails = [l for l, ok in _verdicts if not ok]
    if fails:
        print("BLOCKING GATES (first failure per outcome is what dropped it):")
        for l in fails:
            print(f"  - {l}")
    else:
        print("Every gate passed. If the bot still skipped it, the cause is downstream "
              "of the scanner:\n  max_signals_per_scan truncation, held_token_ids, "
              "bot_status=PAUSED, entry_kill_switch,\n  max_open_positions / "
              "max_total_exposure / max_trades_per_day, or balance.")


if __name__ == "__main__":
    main()
