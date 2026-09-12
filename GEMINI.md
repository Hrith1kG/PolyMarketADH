# PolyMarketADH Project Rules

## Architecture
- This project has two runtime processes: a **Streamlit dashboard** (`dashboard.py`) and a **background bot daemon** (`main.py`/`scanner.py`). Both share `database.py`, `paper_broker.py`, `live_broker.py`, and `settings_manager.py`.
- The bot uses `python-dotenv` to load `.env`. The dashboard is a Streamlit app. Do NOT mix `st.secrets` into shared modules.
- Settings are stored in `settings.json` and managed by `settings_manager.py`. Always use `settings_manager.update_setting()` to modify them.

## SQLite
- All database connections MUST use the `get_connection()` context manager from `database.py`. Never create raw `sqlite3.connect()` calls.
- WAL mode is mandatory. Do not change `journal_mode`.
- Never pass `None` where SQLite expects an integer (e.g., `LIMIT` clause).

## Streamlit Dashboard
- Theme is defined in `.streamlit/config.toml`. Do NOT inject custom CSS targeting internal Streamlit `data-testid` selectors.
- Use `@st.cache_resource` for singleton objects (brokers, clients). Use `@st.cache_data(ttl=N)` for data queries.
- Use `@st.fragment(run_every=...)` for auto-refreshing sections instead of full-page reruns.
- Use Material Symbols (`:material/icon_name:`) instead of emoji for icons in `st.expander`, `st.button`, etc.
- `use_container_width` is deprecated — use `width="stretch"` instead.

## Order Execution
- Polymarket CLOB rejects LIMIT orders < $5. The bot bypasses this by routing small orders as MARKET orders.
- Always check `live_broker.is_no_balance_rejection()` after order failures to distinguish "insufficient funds" from other errors.

## Deployment
- Push from Mac → `git pull` + `sudo systemctl daemon-reload && sudo systemctl restart polymarket-dashboard polymarket-bot` on VPS.
- Always run `daemon-reload` before restart to avoid stale service config warnings.
