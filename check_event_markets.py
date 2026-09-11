"""Checks whether markets embedded in list_events(live=True) carry everything the
scanner needs, or whether each would need a second per-market fetch.

scanner.py reads market.outcomes.{yes,no}.{token_id,price,label}, market.metrics
.{volume,liquidity} and market.state.{closed,accepting_orders,end_date}. Gamma does
not always hydrate nested markets as fully as list_markets does, so this measures
the population of each field before any switch of data source is committed to.

Usage:
    python check_event_markets.py [--events 20]
"""
import argparse
from collections import Counter

import polymarket_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=20)
    args = ap.parse_args()

    client = polymarket_client.get_public_client()
    page = client.list_events(live=True, closed=False, page_size=100).first_page()
    events = list(page.items or [])[: args.events]
    print(f"Sampled {len(events)} live event(s)\n")

    counts = Counter()
    total = 0
    examples = []

    for ev in events:
        for m in (ev.markets or []):
            total += 1
            st, me, out = m.state, m.metrics, m.outcomes
            if st is not None:
                counts["state"] += 1
                if st.closed is not None: counts["state.closed"] += 1
                if st.accepting_orders is not None: counts["state.accepting_orders"] += 1
                if st.end_date is not None: counts["state.end_date"] += 1
            if me is not None:
                counts["metrics"] += 1
                if me.volume is not None: counts["metrics.volume"] += 1
                if me.liquidity is not None: counts["metrics.liquidity"] += 1
            if out is not None:
                counts["outcomes"] += 1
                for side in ("yes", "no"):
                    o = getattr(out, side, None)
                    if o is None:
                        continue
                    counts[f"outcomes.{side}"] += 1
                    if o.token_id: counts[f"outcomes.{side}.token_id"] += 1
                    if o.price is not None: counts[f"outcomes.{side}.price"] += 1
            if len(examples) < 3 and out and getattr(out, "yes", None):
                examples.append((ev, m))

    if not total:
        print("No markets embedded in the sampled events.")
        return

    print(f"{total} embedded market(s)\n-- FIELD POPULATION --")
    for k in ("state", "state.closed", "state.accepting_orders", "state.end_date",
              "metrics", "metrics.volume", "metrics.liquidity", "outcomes",
              "outcomes.yes", "outcomes.yes.token_id", "outcomes.yes.price",
              "outcomes.no", "outcomes.no.token_id", "outcomes.no.price"):
        c = counts[k]
        print(f"  {k:<30} {c:>5}/{total}  ({100.0 * c / total:5.1f}%)")

    print("\n-- SAMPLES --")
    for ev, m in examples:
        sp = ev.sports
        print(f"  event: {ev.title!r}")
        print(f"    live={ev.state.live} period={sp.period!r} score={sp.score!r} "
              f"elapsed={sp.elapsed!r}")
        print(f"    market: {m.question!r}")
        y = m.outcomes.yes
        print(f"      yes: label={y.label!r} price={y.price} token={str(y.token_id)[:20]}...")
        print(f"      volume={m.metrics.volume if m.metrics else None} "
              f"liquidity={m.metrics.liquidity if m.metrics else None}")

    print("\nREAD THIS AS:")
    print("  If token_id and price are ~100%, the scanner can switch to list_events")
    print("  with no extra per-market request. If they are sparse, each candidate")
    print("  needs a get_market call, which changes the cost of a scan considerably.")


if __name__ == "__main__":
    main()
