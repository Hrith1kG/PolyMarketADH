---
name: polymarket-bot-ops
description: >-
  Use this skill when diagnosing why the Polymarket trading bot missed a trade,
  understanding the scanner filter chain, debugging order execution issues,
  configuring gate settings, or deploying updates to the VPS. Trigger on:
  "why no trades", "bot missed", "slippage", "filter", "gate settings",
  "scanner", "order execution", "deploy", "restart bot".
---

# Polymarket Bot Operations & Diagnostics

## Architecture Overview

The bot has two runtime processes sharing the same codebase:
- **Bot daemon** (`main.py` → `scanner.py` → `live_broker.py`): Runs in a loop, scans markets, places orders.
- **Dashboard** (`dashboard.py`): Streamlit UI for monitoring, manual trading, and configuration.

Both read/write to `trades.db` (SQLite) and `state.json`. Settings are in `settings.json`, managed by `settings_manager.py`.

---

## Scanner Filter Chain (13 Gates)

When `scanner.find_opportunities()` runs, every market passes through these filters **in order**. If any gate fails, the market is silently skipped.

| # | Gate | Setting Key | File | What It Checks |
|---|------|------------|------|----------------|
| 1 | Sports filter | `only_sports`, `sports_tag_id` | `scanner.py` L189-190 | Only scan markets with the configured sport tag |
| 2 | Market type | `sports_market_types` | `scanner.py` L191 | Only `moneyline`, `spread`, or `total` |
| 3 | Market closed | — | `scanner.py` L204 | Skip closed markets |
| 4 | Accepting orders | — | `scanner.py` L206 | Skip markets not accepting orders |
| 5 | Resolution window | `min_hours_to_resolution`, `max_days_to_resolution` | `scanner.py` L208-209, fn `_within_resolution_window` | Market must resolve within [min_hours, max_days]. **When Late Game is ON, min_hours is overridden to 0.** |
| 6 | Late Game | `late_game_enabled`, `late_game_threshold_seconds`, `require_authoritative_time` | `scanner.py` L211-213, fn `_late_game_ok` | Game must have started AND resolve within threshold seconds |
| 7 | Volume | `min_volume` | `scanner.py` L222 | Market 24h volume ≥ threshold |
| 8 | Liquidity | `min_liquidity` | `scanner.py` L222 | Market liquidity ≥ threshold |
| 9 | Price range (gamma) | `price_min`, `price_max` | `scanner.py` L237 | Polymarket API price must be within range |
| 10 | Orderbook health | `require_healthy_data` | `scanner.py` L244-252, fn `_orderbook_health_price` | Two-sided book, spread ≤ 3¢, quote age ≤ 5 min |
| 11 | High confidence | `require_high_confidence` | Same function | Stricter: spread ≤ 1¢, age ≤ 60s, depth ≥ stake size |
| 12 | Price range (confirmed) | `price_min`, `price_max` | `scanner.py` ~L270 | CLOB-confirmed price must ALSO be within range |
| 13 | Already held | `held_token_ids` | `scanner.py` L234 | Skip tokens we already have a position in |

After passing the scanner, the bot in `main.py` applies two more checks:
- **Account eligibility**: Each account's risk limits (max positions, max exposure, kill switch, pause status)
- **Slippage guard** (`_slippage_ok` in `main.py` L44-64): Re-checks the live best ask vs the quoted price. If drift > `max_slippage`, the trade is blocked.

## Gate Settings Explained

### Require Healthy Data
- Pulls the live CLOB order book and checks: two-sided, spread ≤ 3¢, quote updated within 5 min.
- **Keep ON** for safety.

### Require High Confidence
- Much stricter: spread ≤ 1¢, quote age ≤ 60s, resting size must fill your entire stake.
- **Very restrictive.** Turn OFF if bot isn't finding trades.

### Late Game Enabled
- Only trade markets where the game has started AND resolves within `late_game_threshold_seconds`.
- When ON, `min_hours_to_resolution` is overridden to 0 (otherwise they conflict).

### Require Authoritative Time
- If ON, refuses to trade any market without a confirmed `game_start_time` from Polymarket's sports feed.
- Many tennis/cricket matches lack this data. **Keep OFF** to avoid missing trades.

## Common "Why No Trades?" Checklist

1. Check `price_min`/`price_max` — late-game favourites are often > $0.95, outside default range.
2. Check if `late_game_enabled` is actually saved (compare dashboard vs `settings.json` on VPS).
3. Check `require_high_confidence` — if ON, most thin markets (tennis, cricket) will fail.
4. Check `require_authoritative_time` — if ON, markets without tagged kickoff times are skipped.
5. Check `late_game_threshold_seconds` — too low means only the final minutes qualify.
6. Check `max_slippage` — if too tight ($0.005), fast-moving markets trigger the slippage guard.
7. Check activity logs for "Entry blocked by slippage guard" or "SKIP" messages.

## Order Execution

- **Polymarket CLOB rejects LIMIT orders < $5** (or < 5 shares).
- The bot bypasses this by routing small orders as **MARKET** orders (using USDC `amount` for buys, `shares` for sells).
- The dashboard's Manual Trade Trigger offers a LIMIT/MARKET selectbox.

## VPS Deployment

```bash
# On Mac: push changes
git add -A && git commit -m "description" && git push

# On VPS: pull and restart
cd ~/polymarketadh
git pull
sudo systemctl daemon-reload
sudo systemctl restart polymarket-dashboard polymarket-bot
```
