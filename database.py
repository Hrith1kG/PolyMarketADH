"""SQLite local database manager for Polymarket Sureshot trades,
providing persistent execution logs, settlement history, and analytics."""
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

DB_FILE = os.getenv("TRADES_DB_FILE", "trades.db")


def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_FILE) -> None:
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                placed_at TEXT,
                market_id TEXT,
                token_id TEXT,
                question TEXT,
                outcome TEXT,
                entry_price REAL,
                tokens REAL,
                cost REAL,
                time_left TEXT,
                result TEXT DEFAULT 'PENDING',
                resolved_price REAL,
                payout REAL,
                pnl REAL DEFAULT 0.0,
                broker TEXT DEFAULT 'paper',
                tx_hash TEXT,
                closed_at TEXT,
                note TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_placed_at ON trades(placed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_id ON trades(token_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_result ON trades(result)")
        conn.commit()


def record_trade(trade: Dict[str, Any], db_path: str = DB_FILE) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO trades (
                trade_id, placed_at, market_id, token_id, question, outcome,
                entry_price, tokens, cost, time_left, result, resolved_price,
                payout, pnl, broker, tx_hash, closed_at, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("trade_id"),
            trade.get("placed_at"),
            trade.get("market_id"),
            trade.get("token_id"),
            trade.get("question"),
            trade.get("outcome"),
            float(trade.get("entry_price", 0.0)),
            float(trade.get("tokens", 0.0)),
            float(trade.get("cost", 0.0)),
            str(trade.get("time_left", "")),
            trade.get("result", "PENDING"),
            float(trade.get("resolved_price", 0.0)) if trade.get("resolved_price") is not None else None,
            float(trade.get("payout", 0.0)) if trade.get("payout") is not None else None,
            float(trade.get("pnl", 0.0)),
            trade.get("broker", "paper"),
            trade.get("tx_hash"),
            trade.get("closed_at"),
            trade.get("note", ""),
        ))
        conn.commit()


def settle_trade(
    token_id: str,
    resolved_price: float,
    payout: float,
    pnl: float,
    result: str,
    closed_at: str,
    note: str = "",
    db_path: str = DB_FILE,
) -> bool:
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            UPDATE trades
            SET resolved_price = ?,
                payout = ?,
                pnl = ?,
                result = ?,
                closed_at = ?,
                note = ?
            WHERE token_id = ? AND result = 'PENDING'
        """, (resolved_price, payout, pnl, result, closed_at, note, token_id))
        conn.commit()
        return cursor.rowcount > 0


def get_all_trades(
    outcome_filter: Optional[str] = None,
    broker_filter: Optional[str] = None,
    limit: int = 200,
    db_path: str = DB_FILE,
) -> List[Dict[str, Any]]:
    init_db(db_path)
    query = "SELECT * FROM trades WHERE 1=1"
    params: List[Any] = []

    if outcome_filter and outcome_filter != "ALL":
        query += " AND outcome = ?"
        params.append(outcome_filter)

    if broker_filter and broker_filter != "ALL":
        query += " AND broker = ?"
        params.append(broker_filter.lower())

    query += " ORDER BY placed_at DESC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_available_outcomes(db_path: str = DB_FILE) -> List[str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT DISTINCT outcome FROM trades WHERE outcome IS NOT NULL AND outcome != '' ORDER BY outcome ASC")
        return [row[0] for row in cursor.fetchall()]


def sync_from_state(state: Dict[str, Any], db_path: str = DB_FILE) -> int:
    """Migrates any existing open positions and closed trades from state.json
    into the SQLite database without duplicating entries."""
    init_db(db_path)
    count = 0

    # 1. Sync open positions
    positions = state.get("positions", {})
    for tid, p in positions.items():
        trade_id = p.get("trade_id") or f"trd_ord_{str(tid)[:8]}"
        trade_data = {
            "trade_id": trade_id,
            "placed_at": p.get("opened_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            "market_id": p.get("market_id", ""),
            "token_id": tid,
            "question": p.get("question", ""),
            "outcome": p.get("outcome_label", ""),
            "entry_price": p.get("entry_price", 0.0),
            "tokens": p.get("shares", 0.0),
            "cost": p.get("stake", 0.0),
            "time_left": "0.0m",
            "result": "PENDING",
            "resolved_price": None,
            "payout": None,
            "pnl": 0.0,
            "broker": p.get("mode", "paper").lower(),
            "tx_hash": p.get("tx_hash"),
            "closed_at": None,
            "note": "",
        }
        record_trade(trade_data, db_path=db_path)
        count += 1

    # 2. Sync closed trades
    closed = state.get("closed_trades", [])
    for t in closed:
        tid = t.get("token_id", "")
        trade_id = t.get("trade_id") or f"trd_ord_{str(tid)[:8]}_{str(t.get('closed_at', ''))[-5:]}"
        pnl = t.get("pnl", 0.0)
        res = "WON" if pnl > 0 else "LOST"
        trade_data = {
            "trade_id": trade_id,
            "placed_at": t.get("opened_at", t.get("closed_at", "")),
            "market_id": t.get("market_id", ""),
            "token_id": tid,
            "question": t.get("question", ""),
            "outcome": t.get("outcome_label", ""),
            "entry_price": t.get("entry_price", 0.0),
            "tokens": t.get("shares", 0.0),
            "cost": t.get("stake", 0.0),
            "time_left": "0.0m",
            "result": res,
            "resolved_price": t.get("resolved_price", 0.0),
            "payout": t.get("payout", 0.0),
            "pnl": pnl,
            "broker": t.get("mode", "paper").lower(),
            "tx_hash": t.get("tx_hash"),
            "closed_at": t.get("closed_at"),
            "note": t.get("note", ""),
        }
        record_trade(trade_data, db_path=db_path)
        count += 1

    return count
