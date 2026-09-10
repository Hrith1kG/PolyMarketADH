# Sureshot Bot — Correctness Audit

Audit of `main.py`, `scanner.py`, `paper_broker.py`, `live_broker.py`, `database.py`,
`settings_manager.py`, `config.py` and `dashboard.py` against `polymarket-client` 0.10.0.

---

## Part 1 — Why the dashboard shows 10 open positions that don't exist on the account

Three separate defects compound into the reported symptom.

### C1 (root cause). Positions are booked on order *submission*, never on *fill*

`AccountSession.place_buy()` (`live_broker.py:135`) returns whatever
`client.place_limit_order()` returns and never looks at it. `place_buy_all` /
`place_buy_selected` then set `"success": True` for any call that didn't raise
(`live_broker.py:272`, `live_broker.py:318`), and `main.py:184` books a full local
position off that flag:

```python
if res["success"]:
    ...
    pos, _ = broker.open_position(opp, stake=stake_used, mode="LIVE", ...)
```

That flag does not mean the order filled. In the SDK,
`place_limit_order() -> OrderResponse = AcceptedOrder | RejectedOrder`
(`polymarket/models/clob/order_response.py`). A CLOB-level rejection is returned as
`RejectedOrder(ok=False, code=...)` — **it does not raise**. The codes include
`unmatched`, `not_enough_balance`, `market_not_ready`, `invalid_nonce`,
`fok_not_filled`, `fak_not_filled`. And an `AcceptedOrder` still carries
`status: "live" | "matched" | "delayed"` — only `matched` means anything filled;
`live` means the order is resting in the book, unfilled.

Nothing in the repo ever reads `.ok`, `.status`, `.making_amount`, `.taking_amount`
or `.trade_ids`:

```
$ grep -rn "\.ok\b|AcceptedOrder|RejectedOrder|taking_amount|making_amount|order_id" *.py
(no matches)
```

So a rejected order and a resting unfilled order are both recorded locally as a
complete, filled position at the full requested size. That is exactly the state in
the screenshot: 10 local rows, $10 "exposure", nothing in the wallet.

Contributing factor: order size ignores the market's minimum. `place_buy` computes
`size = ceil((stake/price) * 100) / 100` with a $1 notional floor and never consults
`order_book.min_order_size` or the depth available at the best ask. With $1 stakes,
sizes land near ~1.05 shares, which the CLOB can reject outright — and per the above,
a rejection is recorded as a fill.

### C2. The Exit path can never remove a stale position from `state.json`

`dashboard.py:1069-1092`. When the sell fails with a balance error, the fallback is:

```python
reconciled = live_inst.reconcile_positions(pos_acc)
if reconciled: ...
else:
    database.force_settle_orphaned_trade(p.get("trade_id"), won=False, ...)
    st.warning("Position already closed on Polymarket. Settled in local database.")
```

`force_settle_orphaned_trade()` writes **only to `trades.db`**. The entry in
`state.json["positions"]` is never touched. The Overview builds its table from
`state.json` positions merged with DB PENDING rows (`dashboard.py:871-899`), so the
`state.json` copy keeps rendering. Second click: the DB row is already settled, so it
is no longer in `reconcile_positions`'s PENDING set, reconcile returns `[]`, and you
land in the same branch again. That is the "it says already exited but it's still
there" loop, verbatim.

### C3. The bot process resurrects anything the dashboard deletes

`main.py:61` creates `broker = PaperBroker()` once and never reloads it.
`PaperBroker.save()` serialises the **entire** in-memory state dict over `state.json`,
and `broker.save_signals(...)` runs every poll (`main.py:130`, default 60s).

The dashboard is a second process with its own `PaperBroker`, and
`reconcile_positions` constructs a third. There is no locking or re-read anywhere —
last full-file write wins. So when the dashboard *does* successfully clear a position,
the bot overwrites `state.json` from its stale snapshot within one poll interval and
the position reappears. This applies to every dashboard-side mutation: exits,
settlements, orphan cleanup, and the portfolio reset.

**Fix direction for Part 1:** gate `open_position` on an `AcceptedOrder` with
`status == "matched"` and size the local position from `making_amount`/`taking_amount`
(track anything else as a working order, not a position); make every exit/settle path
clear `state.json` and `trades.db` together; and give `state.json` a single writer or
a real lock plus re-read-before-write.

---

## Part 2 — P&L is being fabricated

These are the most dangerous findings after C1, because they make the numbers on the
dashboard actively wrong rather than merely stale.

### C4. Any UMA-resolved market is booked as a WIN, including losses

`paper_broker.py:398`:

```python
final_settle_price = 1.0 if (raw_price is not None and raw_price >= 0.5) or is_uma_resolved else 0.0
```

`or is_uma_resolved` overrides the price test. A position whose token resolved to
`0.0` is still settled at `1.0`. The balance is credited, the trade is written to
SQLite as `WON`, and `main.py:99` then fires an on-chain CTF redemption for it.

### C5. Stale positions are auto-settled as wins after 10 hours

`paper_broker.py:419`, the elapsed-time branch: if `end_date` passed more than 10 hours
ago, `final_settle_price = 1.0` unconditionally, noted as
`"settled (match completed at ...)"`. There is no check of the actual outcome. Every
position the resolution lookup couldn't confirm eventually becomes a fabricated win.

### C6. Reconciliation invents wins for orders that never filled

`live_broker.py:448` repeats the `or is_uma` bug. Worse, `live_broker.py:456`:

```python
sell_price = 1.0 if entry_p >= 0.90 else 0.0
```

"Zero balance on-chain and entry was above 0.90, so assume it won." Combined with C1
— where zero on-chain balance is the *normal* state because nothing filled — pressing
**Sync** books the whole phantom book as profitable wins.

This is not opt-in. `dashboard.py:872-875` calls `reconcile_positions()` on **every
Overview render** in LIVE mode, so it runs on page load and on any widget interaction,
not just when the Sync button is pressed.

---

## Part 3 — Other high-severity findings

**H1. Live exits are not fill-verified either.** `dashboard.py:1065` calls
`live_inst.exit_position(...)`, discards the response, books the exit locally and shows
"Sold on CLOB and settled position!". A `RejectedOrder`, or an accepted-but-resting
sell, produces the same success message and the same local P&L entry.

**H2. Manual Trade Trigger can place untracked live orders.**
`dashboard.py:1192-1210` places the order first, then calls `broker.open_position(...)`
and **discards its return value**. If a risk limit rejects it, the order is already on
the exchange with no local record at all. The same block uses `place_buy_all`, which
fires every enabled account and ignores the per-account pause / kill-switch / filters
that `main.py` honours — and it never checks the global `entry_kill_switch`, so the
PANIC button does not block manual entries.

**H3. A simulated balance gates real trading.** `can_open` rejects on
`stake > self.state["balance"]` (`paper_broker.py:170`) and `open_position` does
`self.state["balance"] -= stake` (`paper_broker.py:251`) for LIVE trades too, with
payouts credited back on settle. Live execution is throttled by a fake $1,000 ledger
unrelated to wallet USDC — and C4–C6 keep inflating that ledger with phantom payouts.

**H4. `trade_id` is not unique; history gets silently overwritten.**
`paper_broker.py:206` builds `trd_ord_{account}_{token_id[:8]}`, and
`database.record_trade` uses `INSERT OR REPLACE` on that primary key. Re-entering the
same market on the same account destroys the earlier row, settled or not.

**H5. No order cancellation exists anywhere.** `grep cancel_order` returns nothing.
Unfilled limit orders rest on the CLOB indefinitely, are invisible to every local risk
cap, and can fill hours later at a price the strategy no longer wants.

**H6. "Reset State / Paper Portfolio" deletes LIVE positions.**
`dashboard.py:1952-1966` replaces `positions` with `{}` wholesale. The help text says
"does NOT affect your live wallet" — true of the wallet, but the *tracking* for real
open positions is destroyed, orphaning them.

**H7. Three advertised safety controls do nothing.**
- `max_slippage` — editable in Engine Controls, documented in the README, read by
  nothing. `grep` finds it only in `settings_manager.py` and the two dashboard lines
  that render the input.
- `require_high_confidence` — written by `dashboard.py:778`, never read by `scanner.py`.
- `min_hours_to_resolution` — silently forced to `0.0` whenever Late Game is enabled
  (`scanner.py:136`), while the Circuit Breakers table still reports it as ENFORCED.

---

## Part 4 — Medium findings

| # | Finding |
|---|---|
| M1 | `can_open` uses `limits.get(k) or settings.get(k)` — a per-account limit of `0` is falsy and silently falls back to the global cap. You cannot set an account's cap to zero. |
| M2 | PAPER entries pass `account_name=None` to `can_open`, so `max_open_positions` / exposure count LIVE and PAPER positions together in one bucket. |
| M3 | Sizing ignores `order_book.min_order_size` and the size available at the best ask, so orders can be un-fillable by construction. |
| M4 | `get_all_trades` returns `ORDER BY placed_at DESC`, but `render_pnl_bar_chart` takes `closed[-10:]` and the collaborator table takes `collab_trades[-30:]` — both show the **oldest** rows under a "Last 10 / Recent" label. |
| M5 | Break-even is classified inconsistently: `_close_position` and `summary()` count `pnl == 0` as LOST, `render_pnl_bar_chart` counts it as a Win. |
| M6 | No file locking on `state.json` across the bot process, the dashboard process and `reconcile_positions`' own broker instance. Whole-file writes, last writer wins. |
| M7 | `summary()` scopes every figure by mode except `today_trades`, which stays global. |
| M8 | `scanner.find_opportunities` wraps the whole scan in one bare `except Exception` that `print()`s and returns partial results; the dashboard renders that as "no signals" rather than an error. |
| M9 | `database._SLUG_CACHE` hard-codes ten market IDs from a past session, and `init_db()` re-runs an `UPDATE` for each of them on every call. |
| M10 | `init_db()` is invoked from every `record_trade` / `settle_trade` / `get_all_trades` — three `ALTER TABLE`s, four `CREATE INDEX`es and ten `UPDATE`s per query. |
| M11 | `config.add_account` / `set_account_relayer` write private keys to `.env` with default permissions; the file is never `chmod 600`. |
| M12 | `check_resolutions` issues one uncached `get_market` per open position per loop, with no backoff — a rate-limit trip silently degrades into the C5 "settle as win" path. |

---

## Suggested order of work

1. **C1** — fill verification on buys (and H1 on sells). Nothing else is trustworthy
   until local state reflects what actually executed.
2. **C4, C5, C6** — stop settling unknown outcomes as wins. Settle only on a confirmed
   resolved price for *your* token; otherwise leave PENDING and surface it.
3. **C2, C3** — make exit/settle atomic across `state.json` + `trades.db`, and make
   `state.json` single-writer (or lock + re-read).
4. **H2, H3, H6, H7** — close the manual-trigger bypass, decouple the live path from
   the paper balance, scope the reset to PAPER, and either wire up or remove the
   controls that currently do nothing.
