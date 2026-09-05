# Polymarket "Sureshot" Bot

Scans active Polymarket markets for outcomes priced near-certain (default: 0.97-0.995),
above liquidity/volume floors, and paper-trades them so you can measure hit-rate and P&L
before ever risking real money.

**This is not financial advice, and a 0.97 price is not a guarantee.** Markets do flip
on late news, oracle disputes, or thin-book manipulation. Treat this as a starting point
to backtest/paper-trade your own risk tolerance, not a money machine.

## How it decides a "sureshot"

An outcome is a candidate when, on the latest scan:
- Gamma-reported price is between `PRICE_MIN` and `PRICE_MAX` (default 0.97-0.995)
- Market volume >= `MIN_VOLUME` and liquidity >= `MIN_LIQUIDITY` (filters out thin books
  where one small trade can fake a near-1.0 price)
- Time to resolution is between `MIN_HOURS_TO_RESOLUTION` and `MAX_DAYS_TO_RESOLUTION`
- The price is then re-confirmed against the live CLOB order book (`clob_client.get_price`)
  before a paper trade is opened, since Gamma's cached price can lag the real book

All thresholds live in `.env` (copy `.env.example` -> `.env`) or `config.py`.

## Setup

```bash
python -m venv venv
venv\Scripts\activate      # on Windows
pip install -r requirements.txt
copy .env.example .env
python main.py
```

No wallet, private key, or API key is needed for paper trading -- market data and
order-book prices are public endpoints.

## What it does each cycle

1. Checks open paper positions against Gamma to see if their market has closed/resolved,
   and books realized P&L.
2. Scans all active markets for new candidates, skipping any market it's already holding.
3. Opens a simulated position sized at `STAKE_PER_TRADE`, respecting `MAX_OPEN_POSITIONS`
   and `MAX_TOTAL_EXPOSURE`.
4. Prints a running balance/exposure/W-L summary and sleeps `POLL_INTERVAL_SECONDS`.

State (balance, open positions, closed trade log) persists to `state.json` between runs.
Delete it to reset.

## Going live

`live_broker.py` wraps Polymarket's official `py-clob-client` to place real GTC limit
orders. It is deliberately **not** wired into `main.py` by default. Before touching it:

1. Paper-trade for long enough (weeks, many resolved markets) to trust the hit-rate and
   that fees/slippage don't eat the edge.
2. Understand you need a Polygon wallet funded with USDC.e, and that `PRIVATE_KEY` in
   `.env` is your wallet's private key -- treat that file as a secret, never commit it,
   and prefer a wallet holding only what you're willing to risk in this bot.
3. `pip install -r requirements-live.txt`
4. Set `LIVE_TRADING=true`, `PRIVATE_KEY`, and `FUNDER_ADDRESS` in `.env`.
5. Run `python main.py` -- it will print your configured risk caps and require you to
   type `I UNDERSTAND THE RISK` before placing a single live order.

Even then, keep `STAKE_PER_TRADE` and `MAX_TOTAL_EXPOSURE` small until you've watched it
run live for a while. Nothing here protects you from a market resolving against a 0.99
price -- it happens.

## Files

- `config.py` -- all tunables, loaded from `.env`
- `gamma_client.py` -- public market listing/metadata (Gamma API)
- `clob_client.py` -- public order-book price lookups (CLOB API)
- `scanner.py` -- the "sureshot" filter, returns `Opportunity` objects
- `paper_broker.py` -- simulated portfolio, persisted to `state.json`
- `live_broker.py` -- real order placement via `py-clob-client` (opt-in, see above)
- `main.py` -- the scan/trade/settle loop
