"""Surveys Polymarket sports events to answer: is end_date a usable proxy for when
a game actually finishes, and what live-state fields are actually populated?

Samples events via list_events (which exposes EventState.live / EventSportsMetadata,
none of which the Market model carries) and reports, per sport:

  * how often end_date and start_time are defined at all
  * the distribution of (end_date - start_time) -- if this clusters at whole days,
    end_date is a scheduled expiry, not the match end
  * how often live / ended / game_status / elapsed / period / score are populated

Usage:
    python survey_sports_timing.py              # live events + upcoming, default sample
    python survey_sports_timing.py --pages 5    # widen the sample
"""
import argparse
import statistics
from collections import Counter, defaultdict
from datetime import timezone

import polymarket_client


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def collect(client, label, pages, **kwargs):
    """Pulls up to `pages` pages of events, tolerating an unsupported filter."""
    rows = []
    try:
        paginator = client.list_events(page_size=100, **kwargs)
    except Exception as exc:
        print(f"  !! {label}: {type(exc).__name__}: {exc}")
        return rows
    n = 0
    try:
        for page in paginator:
            n += 1
            if not page.items:
                break
            rows.extend(page.items)
            if n >= pages:
                break
    except Exception as exc:
        print(f"  !! {label}: stopped after {n} page(s): {type(exc).__name__}: {exc}")
    print(f"  {label}: {len(rows)} event(s) over {n} page(s)")
    return rows


def sport_of(ev):
    sp = ev.sports
    if sp and sp.sport and getattr(sp.sport, "sport", None):
        return sp.sport.sport
    if sp and sp.series_slug:
        return sp.series_slug.split("-")[0]
    return ev.subcategory or ev.category or "unknown"


def survey(events, title):
    if not events:
        print(f"\n### {title}: no events sampled")
        return

    print(f"\n{'=' * 78}\n### {title}  (n={len(events)})\n{'=' * 78}")

    have_end = have_start = 0
    deltas = []
    field_counts = Counter()
    per_sport = defaultdict(lambda: {"n": 0, "end": 0, "start": 0, "deltas": []})
    whole_day = 0

    for ev in events:
        sch, st, sp = ev.schedule, ev.state, ev.sports
        sport = sport_of(ev)
        b = per_sport[sport]
        b["n"] += 1

        end = _aware(sch.end_date if sch else None)
        start = _aware((sch.start_time if sch else None) or (sch.start_date if sch else None))
        if end:
            have_end += 1
            b["end"] += 1
        if start:
            have_start += 1
            b["start"] += 1
        if end and start:
            h = (end - start).total_seconds() / 3600.0
            deltas.append(h)
            b["deltas"].append(h)
            # A real match end lands hours after start; a scheduled expiry lands on a
            # round day boundary. Count how many sit within a minute of a whole day.
            if abs((h * 3600) % 86400) < 60 or abs(((h * 3600) % 86400) - 86400) < 60:
                whole_day += 1

        for name, val in (
            ("state.live", st.live if st else None),
            ("state.ended", st.ended if st else None),
            ("state.closed", st.closed if st else None),
            ("schedule.start_time", sch.start_time if sch else None),
            ("schedule.end_date", sch.end_date if sch else None),
            ("schedule.finished_at", sch.finished_at if sch else None),
            ("sports.game_status", sp.game_status if sp else None),
            ("sports.elapsed", sp.elapsed if sp else None),
            ("sports.period", sp.period if sp else None),
            ("sports.score", sp.score if sp else None),
        ):
            if val is not None and val != "":
                field_counts[name] += 1

    n = len(events)
    print(f"\n-- FIELD POPULATION (how often it is set at all) --")
    for name in ("state.live", "state.ended", "state.closed", "schedule.start_time",
                 "schedule.end_date", "schedule.finished_at", "sports.game_status",
                 "sports.elapsed", "sports.period", "sports.score"):
        c = field_counts[name]
        print(f"  {name:<24} {c:>5}/{n}  ({100.0 * c / n:5.1f}%)")

    print(f"\n-- IS end_date THE MATCH END? --")
    print(f"  end_date defined:   {have_end}/{n} ({100.0 * have_end / n:.1f}%)")
    print(f"  start defined:      {have_start}/{n} ({100.0 * have_start / n:.1f}%)")
    if deltas:
        deltas.sort()
        print(f"  end_date - start, in hours, over {len(deltas)} event(s):")
        print(f"    min={deltas[0]:.2f}  p25={deltas[len(deltas)//4]:.2f}  "
              f"median={statistics.median(deltas):.2f}  "
              f"p75={deltas[3*len(deltas)//4]:.2f}  max={deltas[-1]:.2f}")
        print(f"    landing on a whole-day boundary: {whole_day}/{len(deltas)} "
              f"({100.0 * whole_day / len(deltas):.1f}%)")
        buckets = Counter()
        for h in deltas:
            if h < 4: buckets["< 4h (plausible match end)"] += 1
            elif h < 24: buckets["4-24h"] += 1
            elif h < 24 * 7: buckets["1-7 days"] += 1
            else: buckets["> 7 days"] += 1
        for k, v in buckets.most_common():
            print(f"    {k:<32} {v:>5} ({100.0 * v / len(deltas):5.1f}%)")
    else:
        print("    (no event had both end_date and a start time)")

    print(f"\n-- PER SPORT --")
    print(f"  {'sport':<22} {'n':>5} {'end_date':>9} {'start':>7} {'median gap':>11}")
    for sport, b in sorted(per_sport.items(), key=lambda kv: -kv[1]["n"])[:15]:
        med = f"{statistics.median(b['deltas']):.1f}h" if b["deltas"] else "-"
        print(f"  {sport:<22} {b['n']:>5} {b['end']:>9} {b['start']:>7} {med:>11}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3, help="pages of 100 per sample")
    ap.add_argument("--tag-id", type=int, default=None, help="restrict to a tag id")
    args = ap.parse_args()

    client = polymarket_client.get_public_client()
    tag = {"tag_ids": args.tag_id} if args.tag_id else {}

    print("Sampling events...")
    live = collect(client, "live=True, closed=False", args.pages,
                   live=True, closed=False, **tag)
    upcoming = collect(client, "closed=False (any)", args.pages, closed=False, **tag)
    done = collect(client, "closed=True (resolved)", args.pages, closed=True, **tag)

    survey(live, "LIVE EVENTS (state.live=True)")
    survey(upcoming, "OPEN EVENTS (closed=False)")
    survey(done, "RESOLVED EVENTS (closed=True)")

    print(f"\n{'=' * 78}")
    print("READ THIS AS:")
    print("  * If 'end_date defined' is ~100% but the median gap is >= 24h and most")
    print("    land on a whole-day boundary, end_date is a SCHEDULED EXPIRY and cannot")
    print("    be used to tell when a game finishes.")
    print("  * If state.live / sports.game_status are well populated, filtering on")
    print("    those is the correct way to select in-play games.")
    print("  * Note these fields exist on Event, NOT on Market. scanner.py currently")
    print("    calls list_markets, so none of them are visible to it today.")


if __name__ == "__main__":
    main()
