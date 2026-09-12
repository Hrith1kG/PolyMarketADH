# Late Game timing: live-event payload and per-sport rules

This documents the evidence the Late Game strategy is built on, gathered by sampling
the live Gamma `/events` payload (37 live events plus targeted per-league probes).
`live_timing.py` implements exactly what is described here.

## Why `end_date` was removed from the decision

`end_date` does not mark when a match finishes, and what it *does* mean changes per
sport. Observed on live events:

| Sport | `startTime` | `endDate` | `endDate - startTime` |
|---|---|---|---|
| Soccer (`kor`, `ukr1`, `chi1`) | `2026-09-12T10:00Z` | `2026-09-12T10:00Z` | **0** |
| College football (`cfb`) | `2026-09-11T23:30Z` | `2026-09-11T23:30Z` | **0** |
| Esports (`cs2`, `lol`, `dota2`, `val`) | `2026-09-12T08:10Z` | `2026-09-12T14:10Z` | **+6h** |
| Tennis (`atp`, `wta`) | `2026-09-12T08:55Z` | `2026-09-19T09:00Z` | **+7d** |
| Baseball (`mlb`) | `2026-09-12T02:15Z` | `2026-09-19T02:15Z` | **+7d** |
| Cricket (`crint`) | `2026-09-09T10:00Z` | `2026-09-16T06:00Z` | **+7d** |

A single `end_date` threshold therefore admits soccer and football at *any* point in
the match (their `end_date` is already in the past) while permanently rejecting
tennis, baseball and cricket. That is the bug this rewrite removes. `end_date` is no
longer read by any Late Game code path.

## Where live state comes from

Live state lives on the **Event**, not the Market. `list_markets` cannot see it at
all, so the Late Game scan path uses the server-side live filter:

```python
client.list_events(live=True, closed=False, page_size=100)
```

Markets embedded in those events are fully hydrated for what the scanner needs —
measured over 220 embedded markets: `outcomes.{yes,no}.token_id` 100%,
`outcomes.{yes,no}.price` 100%, `state.end_date` 100%, `metrics.liquidity` 64.5%.
No extra per-market fetch is required to evaluate a candidate.

An event is treated as authoritatively live only when `event.state.live is True` and
`event.state.ended` is not True.

## The timing fields

Only three in-play fields exist on `EventSportsMetadata`, all untyped strings:

| Field | Populated for | Example values |
|---|---|---|
| `period` | nearly all live events | `1H`, `2H`, `SUS`, `NS`, `S1`, `S2`, `3/3`, `2/5`, `Live`, `VFT` |
| `elapsed` | **soccer only** | `"14"`, `"19"` (whole minutes since kickoff) |
| `score` | nearly all live events | `0-0`, `3-6, 4-5`, `6-1\|1-1\|Bo3`, `453-185` |

`game_status`, `finished_at` and any dedicated clock field are `None` on live events.
There is no `secondsRemaining` anywhere in the raw payload. So: soccer has a real
clock, esports has a countable series position, and everything else has a period
label and nothing more.

## Sport family resolution

Two layers, because league tagging is not uniform.

1. **Explicit sport code** (`event.sports.sport.sport`) for leagues whose family
   cannot be inferred from tags: `nba`, `wnba`, `ncaab`, `nfl`, `cfb`, `nhl`, `mlb`.
   Their `sport.tags` carry only their own league tag (e.g. NFL is `1,450,100639`),
   never a family tag, so they must be named.
2. **Family tag id**, which covers the long tail without enumerating every league:

   | Tag | Family | Confirmed on |
   |---|---|---|
   | `100350` | Soccer | `kor`, `ukr1`, `chi1` |
   | `64` | Esports | `cs2`, `lol`, `dota2`, `val`, `r6siege`, `mlbb`, `hok` |
   | `864` | Tennis | `atp`, `wta`, `wta-doubles` |
   | `517` | Cricket | `crint` |

Anything matching neither layer is **skipped and logged by sport code**, never
guessed. The log line names the code and the observed `period` so a new sport can be
added deliberately.

## Per-sport timing rules

All estimates are **wall-clock minutes until the match actually ends**, including
stoppages, intermissions and broadcast overhead — not game-clock minutes. A 30-minute
limit therefore means the same thing to a trader in every sport.

### Soccer — real clock (`CLOCK`)

`elapsed` is whole minutes played. `period` says which half.

```
1H, elapsed=e   ->  (45 - e) + HALFTIME_MINUTES + 45, stretched for stoppage
2H, elapsed=e   ->  (90 - e), stretched for stoppage
HT              ->  45 + HALFTIME remaining
```

Regulation 90, halftime 15, in-play stretch factor 1.15 (stoppage time, VAR, subs),
so a full match is ~115 wall minutes. Extra time and penalties (`ET1`, `ET2`, `PEN`)
add their own fixed allowances. `SUS` (suspended) and `NS` (not started) are **not**
timing states — they are skipped with a reason, as is a `2H` with a missing or
non-numeric `elapsed`.

### Period sports with no in-period clock — conservative worst case (`PERIOD`)

`nba`, `wnba`, `ncaab`, `nfl`, `cfb`, `nhl`. `period` gives `Q1`–`Q4` / `P1`–`P3` /
`H1`–`H2` / `OT`, and there is no clock within the period. The estimate therefore
assumes **the entire current period is still to play**, plus every period after it,
plus remaining intermissions. This never enters too early; it does mean a sport whose
worst-case final period is longer than the configured limit will not trade until that
limit is raised for it.

| Sport | Periods | Wall minutes per period | Intermission | Full match |
|---|---|---|---|---|
| `nba` | 4 × Q | 30 | 15 at half | ~140 |
| `wnba` | 4 × Q | 26 | 15 at half | ~120 |
| `ncaab` | 2 × H | 50 | 15 at half | ~120 |
| `nfl` | 4 × Q | 42 | 13 at half | ~190 |
| `cfb` | 4 × Q | 47 | 20 at half | ~210 |
| `nhl` | 3 × P | 40 | 18 between | ~150 |

Worst case in the final period is thus 30 (NBA), 42 (NFL), 40 (NHL) wall minutes. At
a 30-minute absolute limit only NBA clears, which is why each sport carries its own
overridable `max_remaining_minutes` (see configuration below).

Setting `allow_worst_case` to false for a sport makes it skip instead of estimating.

### Esports — series position (`SERIES`)

`period` is `"<current map>/<maps in series>"` (`3/3`, `2/5`). `score` is
`"<map score>|<series score>|Bo<N>"`, e.g. `6-1|1-1|Bo3`.

Maps still to be played is derived from the **series score**, not the map number: a
Bo3 at 1-1 is decided by the current map alone, so one map remains regardless of
whether `period` reads `3/3`.

The count is the worst case, meaning the most maps the series can still run:

```
needed    = best_of // 2 + 1
maps_left = (needed - wins_a) + (needed - wins_b) - 1
```

Taking only the leader's shortfall would understate this badly — a Bo5 at 1-0 can
still run four more maps even though the leader needs just two. Remaining is
`maps_left ×` that title's typical map duration, with the current map counted in
full, since its progress within the map is not published.

A series' full duration, for the proportional limit, is the longest it can run:
`(2 × needed - 1) × map_minutes`. A Bo1 of 20-minute maps is a 20-minute contest, not
a 90-minute one, so its late-game window scales accordingly.

| Title | Typical map (wall min) |
|---|---|
| `cs2` | 40 |
| `dota2` | 42 |
| `val` | 40 |
| `r6siege` | 35 |
| `lol` | 33 |
| `mlbb` | 22 |
| `hok` | 20 |
| other esports | 35 |

### Clockless formats — skipped (`UNSUPPORTED`)

**Tennis** (`atp`, `wta`, `wta-doubles`): `period` is `S1`–`S5`, `score` is set/game
scores. A set has no bounded duration and a match has no bounded set count within the
best-of, so remaining wall time cannot be estimated to any useful accuracy. Skipped
with reason `tennis has no bounded remaining duration`.

**Cricket** (`crint`): `period` is the constant string `Live` for a five-day Test.
There is no in-play progress signal at all. Skipped with reason
`cricket exposes no in-play progress`.

**Baseball** (`mlb`): innings are unbounded and no in-play inning field was observed
(`period` was `VFT` on every completed sample, `None` before start). Skipped unless a
recognisable inning period appears.

### Terminal and non-playing periods

`VFT`, `FT`, `AOT`, `AP`, `Final` → match over; `NS`, `PRE` → not started;
`SUS`, `POSTP`, `CANC`, `INT`, `DELAY` → not currently playing. All are skipped with
their own reason rather than being read as "almost finished".

## How the limit is applied

The user-facing rule is one threshold, but a fixed minute count means different things
in a 210-minute football game and a 20-minute esports map. The effective limit is
therefore the tighter of an absolute cap and a proportion of that sport's own typical
full duration:

```
effective_limit = min(
    late_game_max_remaining_minutes,
    late_game_max_remaining_fraction * full_match_wall_minutes,
)
```

With the defaults (30 minutes, 0.34) that gives soccer ~30 min of its ~115, a
CS2 Bo1 ~13 min of its ~40, and an NBA game ~30 min of its ~140. Set the fraction to
`0` to disable the proportional component and use the absolute cap alone.

## Configuration

Global settings (all dashboard-editable):

```python
late_game_enabled = False              # off by default
late_game_max_remaining_minutes = 30.0 # absolute wall-clock cap
late_game_max_remaining_fraction = 0.34 # proportional cap; 0 disables
late_game_min_probability = 0.90       # entry band floor
late_game_max_probability = 0.99       # entry band ceiling
late_game_allow_worst_case_periods = True  # global switch for PERIOD sports
late_game_sport_rules = {}             # per-sport overrides
```

Per-sport overrides are keyed by sport code and may set `enabled`,
`allow_worst_case`, `max_remaining_minutes` and `max_remaining_fraction`:

```json
{
  "late_game_sport_rules": {
    "nfl": {"max_remaining_minutes": 45},
    "nhl": {"enabled": false},
    "cs2": {"max_remaining_fraction": 0.25}
  }
}
```

While Late Game is enabled, `late_game_min_probability` / `late_game_max_probability`
replace `price_min` / `price_max` as the entry band, so a 0.90–0.92 band means exactly
that and nothing silently overrides it.

## Separation of concerns

Quote freshness and game timing are deliberately independent. Order-book timestamps
(`HEALTH_MAX_QUOTE_AGE_SECONDS`, `CONFIDENCE_MAX_QUOTE_AGE_SECONDS` in `scanner.py`)
only establish that market data is current. They say nothing about how far the match
has progressed, and no timing decision reads them.

## Rejection reasons

Every rejected candidate is logged with a machine-readable reason, surfaced through
`scanner.LAST_SCAN_REJECTIONS`:

| Reason | Meaning |
|---|---|
| `not_live` | event is not authoritatively live |
| `sport_unsupported` | no rule for this sport code; names the code and period |
| `timing_unavailable` | rule exists but this state yields no reliable estimate |
| `too_much_time_remaining` | estimate exceeds the effective limit |
| `probability_out_of_band` | token price outside the configured band |
| `market_filtered` | wrong market type, closed, or not accepting orders |
| `liquidity_or_volume` | below the liquidity/volume floors |
| `already_held` | a position in this token already exists |
| `unhealthy_book` | spread, two-sidedness, depth or quote-freshness check failed |
