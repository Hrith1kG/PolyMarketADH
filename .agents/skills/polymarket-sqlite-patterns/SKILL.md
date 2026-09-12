---
name: polymarket-sqlite-patterns
description: >-
  Use this skill when modifying database.py, adding new database queries,
  debugging SQLite locking or "database is locked" errors, or wrapping DB
  calls with Streamlit caching. Trigger on: "database", "sqlite", "locked",
  "WAL", "get_connection", "trades.db", "IntegrityError", "datatype mismatch".
---

# SQLite Concurrency Patterns for PolyMarketADH

## Context

This codebase has **two processes** hitting the same `trades.db` simultaneously:
1. The **Streamlit dashboard** — re-executes `dashboard.py` top-to-bottom on every click/interaction.
2. The **background bot daemon** — continuously writes trade records, position updates, and logs.

Without careful connection management, this causes `sqlite3.OperationalError: database is locked`.

---

## Mandatory Patterns

### 1. Always use the context manager for connections

```python
# database.py — get_connection() is a @contextlib.contextmanager
with get_connection() as conn:
    cursor = conn.execute("SELECT ...")
    rows = cursor.fetchall()
# Connection is automatically closed here, even on exceptions
```

**NEVER** do this:
```python
conn = sqlite3.connect(DB_FILE)  # ❌ raw connection, never closed
```

### 2. WAL mode is mandatory

`get_connection()` enables these PRAGMAs on every connection:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

**Do not change or remove these.** WAL (Write-Ahead Logging) allows concurrent readers while a writer is active. Without it, the dashboard freezes for 5+ seconds waiting for the bot's write lock.

### 3. Never pass None to LIMIT

SQLite's `LIMIT ?` parameter binding requires an integer. Passing `None` causes:
```
sqlite3.IntegrityError: datatype mismatch
```

**Always provide a default integer:**
```python
# ✅ Correct
def get_db_trades(limit: int = 200, ...):
    ...

# ❌ Wrong — None flows to LIMIT ?
def get_db_trades(limit: int = None, ...):
    ...
```

### 4. Streamlit caching for DB calls

When wrapping database calls with `@st.cache_data`:
- Use `ttl=10` (10-second cache) so stale data doesn't persist.
- Use `show_spinner=False` to avoid UI flicker.
- Ensure all default argument values match the underlying function's expected types (especially `limit`).

```python
@st.cache_data(ttl=10, show_spinner=False)
def get_db_trades(limit: int = 200, broker_filter: str = None, account_filter: str = None):
    return database.get_all_trades(limit=limit, broker_filter=broker_filter, account_filter=account_filter)
```

### 5. Singleton brokers with cache_resource

```python
@st.cache_resource
def get_broker() -> PaperBroker:
    return PaperBroker()
```

This prevents re-initializing the broker (which reads `state.json` and acquires file locks) on every single Streamlit interaction.
