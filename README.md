# Polymarket "Sureshot" Bot & Control Dashboard

Runs two independent strategies against Polymarket:

1. **Sports Moneyline** ("Sureshot") -- scans active markets for outcomes priced near-certain
   (default: 0.97-0.995) above liquidity/volume floors, focusing exclusively on match winner lines.
2. **Crypto 5-Minute** -- trades live **Up or Down - 5 Min** rounds for BTC, ETH, SOL, XRP and DOGE
   in the final seconds before each round resolves.

Both paper-trade (or live-trade) through the same brokers and risk controls, but they are separate
strategies end to end: separate settings, separate candidate discovery, separate timing rules and
separate polling cadences.

Includes a real-time **Streamlit Control Panel** as the primary interface for live monitoring and runtime adjustments.

Powered by the official unified **`polymarket-client`** Python SDK.

**This is not financial advice, and a 0.97 price is not a guarantee.** Markets do flip
on late news, oracle disputes, or thin-book manipulation. Treat this as a starting point
to backtest/paper-trade your own risk tolerance, not a money machine.

---

## Features

- **Sports & Moneyline Exclusivity**: Scans Polymarket's master sports tag (`tag_id=100639`) and filters for match winner lines (`sports_market_types=["moneyline"]`).
- **Crypto 5-Minute Strategy** (see its own section below): fixed 5-minute Up/Down rounds for five approved coins, entered only inside a configurable end-of-round window at or above a probability floor, with one entry per market/round enforced across restarts.
- **Streamlit Control Panel (`dashboard.py`)**:
  - **Execution Pipeline**: Order lifecycle metrics (intentions, filled, rejected), and tracked positions table with `Mode` tag (`PAPER` / `LIVE`).
  - **Operations & Health**: Lifecycle state cards and Circuit Breakers & Gates enforcement table.
  - **Live Control**: Seamless zero-restart Paper $\leftrightarrow$ Live execution switching, Entry Kill Switch, and real-time live account vitals (wallet, type, on-chain USDC.e collateral balance, open CLOB orders).
  - **Market Signals & Manual Trigger**: Real-time sports moneyline opportunities feed with 1-click manual execution.
  - **Trade History & Performance**: Settled trades log, win-rate tracking, realized P&L, and safety portfolio reset controls.
- **Risk Management**: Enforces max trades per day, max open positions, max exposure, entry slippage limits, and emergency kill-switches (the kill switch blocks manual entries too).
- **CLOB Verification**: Re-confirms Gamma-reported prices against the live CLOB order book before entering trades, and re-checks the best ask against the slippage cap immediately before firing.
- **Fill Verification**: A position is only recorded once the exchange reports shares actually filled, sized from the realized fill. Exits sell the balance the wallet actually holds, so they don't leave fractional dust behind. Orders that are rejected, or accepted but left resting in the book, are surfaced as working orders (cancellable from **Control and Risk**) rather than tracked as positions.
- **Honest Settlement**: Trades settle only against a confirmed outcome price for the specific outcome token held. Positions whose outcome cannot be established are left `PENDING` and flagged for review; locally recorded trades that never executed on-chain are marked `VOID` and excluded from P&L and win rate.

---

## Crypto 5-Minute Strategy

A second, fully separate strategy that trades Polymarket's fixed **"Up or Down - 5 Min"** rounds.
It shares the brokers, the wallet and the trade ledger with the sports strategy, and nothing else:
its settings are all namespaced `crypto_*`, it has its own candidate discovery, its own risk budget
and its own kill switch, and it runs on its own thread so its cadence is independent of the sports
scan interval.

### What it trades

A crypto entry requires **all** of the following:

1. **Approved asset and exact market shape.** The underlying must be one of **BTC, ETH, SOL, XRP,
   DOGE**, identified from whole slug segments (market, event) rather than loose title matching. The
   market's two outcomes must be labelled exactly **Up** and **Down**, and the round's measured
   length must be five minutes -- derived from the start/end timestamps, or from an explicit
   5-minute marker in the slug when Gamma did not hydrate a start time. A round whose length cannot
   be established is treated as ambiguous and skipped; an hourly "up or down" market for the same
   coin is rejected on duration.
2. **Live, and inside the entry window.** The market must be open and accepting orders, and the
   authoritative round-end timestamp must give
   `0 < seconds_remaining <= crypto_entry_window_seconds` (default 30). Expired, future and
   ambiguous rounds are never traded.

   Using the fixed round-end timestamp is sound *here* precisely because the five-minute duration is
   verified first. The sports late-game rule is untouched and still infers "the game is nearly over"
   from a confirmed kickoff plus an imminent resolution, because a match has no fixed length.
3. **Executable price.** The side being bought -- Up or Down, judged independently -- must have a
   live CLOB **best ask** at or above `crypto_min_probability` (default 0.90) and at or below
   `crypto_max_probability` (default 0.999, so a fill with no profit left in it is refused). The
   cached Gamma quote is only ever used to decide whether polling that side's book is worth a
   request; it is never the price the decision is made on.
4. **Every other gate.** Two-sided book, spread within `crypto_max_spread`, quote no older than
   `crypto_max_quote_age_seconds`, enough resting size at the ask to fill the intended stake, the
   crypto risk budget, the per-account risk caps, no duplicate position, and the slippage cap.

### How an entry is made

    scan -> claim the round -> re-check -> submit -> confirm or release

The round is **claimed atomically before anything else acts on it**, in a ledger persisted to
`state.json`, so one entry per market/round holds across repeated scans, concurrent threads,
a separate dashboard process and a restart. Immediately before the order is submitted, market
status, seconds remaining, the executable ask, slippage and every risk check are **re-read and
re-verified** -- a five-minute round can change completely between a scan and an order. A claim
behind a submitted order (filled *or* left resting) is kept forever; a claim that never produced an
order is released so a later poll inside the same window can try again.

Every refusal is logged with a machine-readable reason: `unapproved_asset`, `asset_not_selected`,
`not_up_down_market`, `wrong_round_duration`, `ambiguous_round_duration`, `not_live`,
`round_expired`, `outside_entry_window`, `price_below_threshold`, `price_above_ceiling`,
`book_one_sided`, `book_spread_too_wide`, `book_quote_stale`, `book_depth_insufficient`,
`already_traded_this_round`, `duplicate_position`, `risk_limit`, `slippage`. Repeats of the same
message are throttled so a 3-second cadence does not bury the log.

### Settings

Configured from the dashboard's **Crypto 5m** sidebar tab, or directly in `settings.json`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `crypto_enabled` | `false` | Master switch for the strategy |
| `crypto_bot_status` | `RUNNING` | `PAUSED` stops crypto scanning only |
| `crypto_entry_kill_switch` | `false` | Blocks crypto entries only |
| `crypto_assets` | `["BTC","ETH","SOL","XRP","DOGE"]` | Selected subset of the approved universe |
| `crypto_entry_window_seconds` | `30` | Enter only while `0 < seconds_remaining <= this` |
| `crypto_min_probability` | `0.90` | Floor on the executable ask of the side bought |
| `crypto_max_probability` | `0.999` | Ceiling on that ask |
| `crypto_poll_interval_seconds` | `3` | Crypto cadence; sports keeps `poll_interval_seconds` |
| `crypto_stake_per_trade` | `25.0` | Stake per crypto entry, per account |
| `crypto_max_open_positions` / `crypto_max_total_exposure` / `crypto_max_trades_per_day` | `5` / `100.0` / `20` | Crypto-only risk budget |
| `crypto_max_slippage` | `0.01` | Refuses an entry whose ask ran away from the quote |
| `crypto_max_spread` / `crypto_max_quote_age_seconds` / `crypto_min_ask_depth_multiple` | `0.05` / `20.0` / `1.0` | Order-book health for the side bought |
| `crypto_round_duration_seconds` / `crypto_round_duration_tolerance_seconds` | `300` / `20` | The round shape that defines "a 5-minute market" |
| `crypto_discovery_lookahead_seconds` | `420` | How far past the window to look for upcoming rounds |
| `crypto_discovery_tag_id` | `null` | Optional Gamma tag id to narrow discovery |
| `crypto_order_type` | `LIMIT` | `LIMIT` or `MARKET` |

The crypto strategy starts automatically with `python main.py`, on its own thread, and does nothing
at all (not even a request) until `crypto_enabled` is switched on.

---

## Setup

The unified SDK requires **Python >= 3.11**. We recommend using `uv` or Python 3.12:

```bash
# Using uv (recommended):
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt

# Or using standard python (Python 3.11+):
python3 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
```

---

## How to Run

### 1. Launch the Control Panel Dashboard
Open the primary UI to view signals, monitor positions, and adjust thresholds:
```bash
streamlit run dashboard.py
```
This opens the web interface in your browser at `http://localhost:8501`.

### 2. Run the Automated Trading Bot
In a separate terminal tab (with `.venv` activated):
```bash
python main.py
```
The bot executes automated scan and settlement loops, applying the threshold settings configured in the dashboard in real time.

---

## Files

- `dashboard.py` -- Streamlit web control panel and real-time monitor
- `settings_manager.py` -- dynamic runtime settings provider (`settings.json`)
- `scanner.py` -- the sports moneyline filter, returns `Opportunity` objects
- `crypto_markets.py` -- the crypto asset registry and the pure round classification/timing rules
- `crypto_scanner.py` -- crypto candidate discovery and order-book qualification, returns `CryptoOpportunity` objects
- `crypto_strategy.py` -- the crypto scan/re-check/execute loop and its own polling cadence
- `paper_broker.py` -- simulated portfolio, daily limits, persisted to `state.json`
- `live_broker.py` -- real order placement via `SecureClient` (opt-in)
- `polymarket_client.py` -- unified `PublicClient` and `SecureClient` provider
- `config.py` -- baseline configurations loaded from `.env`
- `main.py` -- the automated scan/trade/settle loop
- `tests/` -- regression suite for execution correctness (`./tests/run_all.sh`)
