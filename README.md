# Polymarket "Sureshot" Bot & Control Dashboard

Scans active Polymarket markets for outcomes priced near-certain (default: 0.97-0.995),
above liquidity/volume floors, focusing exclusively on **Sports Moneyline** matches, and paper-trades
them with full risk controls.

Includes a real-time **Streamlit Control Panel** as the primary interface for live monitoring and runtime adjustments.

Powered by the official unified **`polymarket-client`** Python SDK.

**This is not financial advice, and a 0.97 price is not a guarantee.** Markets do flip
on late news, oracle disputes, or thin-book manipulation. Treat this as a starting point
to backtest/paper-trade your own risk tolerance, not a money machine.

---

## Features

- **Sports & Moneyline Exclusivity**: Scans Polymarket's master sports tag (`tag_id=100639`) and filters for match winner lines (`sports_market_types=["moneyline"]`).
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
- `paper_broker.py` -- simulated portfolio, daily limits, persisted to `state.json`
- `live_broker.py` -- real order placement via `SecureClient` (opt-in)
- `polymarket_client.py` -- unified `PublicClient` and `SecureClient` provider
- `config.py` -- baseline configurations loaded from `.env`
- `main.py` -- the automated scan/trade/settle loop
- `tests/` -- regression suite for execution correctness (`./tests/run_all.sh`)
