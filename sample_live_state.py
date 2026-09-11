"""Dumps the raw period / score / elapsed strings Polymarket reports for live games,
grouped by sport.

These are untyped strings in the SDK (str | None, no documented vocabulary), so any
"the game is nearly over" rule has to be written against the values actually
observed. This prints them per sport, plus how long each game has been underway,
so a late-stage rule can be written from evidence instead of assumption.

Usage:
    python sample_live_state.py
"""
from collections import defaultdict
from datetime import datetime, timezone

import polymarket_client


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def sport_of(ev):
    sp = ev.sports
    if sp and sp.sport and getattr(sp.sport, "sport", None):
        return sp.sport.sport
    if sp and sp.series_slug:
        return sp.series_slug
    return ev.subcategory or ev.category or "unknown"


def main():
    client = polymarket_client.get_public_client()
    page = client.list_events(live=True, closed=False, page_size=100).first_page()
    events = list(page.items or [])
    now = datetime.now(timezone.utc)
    print(f"{len(events)} live event(s) at {now.isoformat()}\n")

    by_sport = defaultdict(list)
    for ev in events:
        by_sport[sport_of(ev)].append(ev)

    for sport, evs in sorted(by_sport.items(), key=lambda kv: -len(kv[1])):
        print("=" * 78)
        print(f"{sport}  (n={len(evs)})")
        print("=" * 78)
        for ev in evs:
            sp, sch = ev.sports, ev.schedule
            start = _aware(sch.start_time if sch else None)
            mins = (now - start).total_seconds() / 60.0 if start else None
            age = f"{mins:7.1f}m in" if mins is not None else "  start? "
            print(f"  {age} | period={sp.period!r:<14} elapsed={sp.elapsed!r:<10} "
                  f"score={sp.score!r}")
            print(f"            {ev.title!r}")
        print()

    print("=" * 78)
    print("DISTINCT VALUES PER SPORT")
    print("=" * 78)
    for sport, evs in sorted(by_sport.items()):
        periods = sorted({e.sports.period for e in evs if e.sports.period})
        elapsed = sorted({e.sports.elapsed for e in evs if e.sports.elapsed})
        print(f"  {sport}")
        print(f"    period : {periods}")
        print(f"    elapsed: {elapsed}")

    print("\nREAD THIS AS:")
    print("  * The period vocabulary is what a late-stage rule must match on, and it")
    print("    is per-sport. Anything with no usable period needs the price-stability")
    print("    gate instead, which is sport-agnostic.")
    print("  * 'minutes in' shows whether time-since-start is a workable clock for")
    print("    that sport (it is not, for tennis or cricket).")


if __name__ == "__main__":
    main()
