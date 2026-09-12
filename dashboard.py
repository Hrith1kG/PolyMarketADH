"""Streamlit Primary Control Panel for Polymarket Sureshot Trading Bot.
Supports seamless switching between Paper and Live Execution, Live Account Vitals,
and full strategy lifecycle monitoring."""
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import altair as alt
import pandas as pd
import streamlit as st

from dotenv import load_dotenv
load_dotenv(override=True)

import config


def _parse_sport_rules(text, fallback):
    """Parses the per-sport override editor, keeping the saved rules if it is invalid."""
    try:
        parsed = json.loads(text or "{}")
    except (ValueError, TypeError):
        st.warning("Per-sport overrides are not valid JSON - keeping the previous rules.")
        return fallback or {}
    if not isinstance(parsed, dict):
        st.warning("Per-sport overrides must be a JSON object - keeping the previous rules.")
        return fallback or {}
    return parsed

import paper_broker
from paper_broker import PaperBroker
import database
import live_broker
import scanner
import settings_manager

# NOTE: modules are intentionally NOT importlib.reload()'d here. Streamlit already
# re-executes this script top-to-bottom on every interaction; reloading every
# imported module on top of that re-ran their setup code and wiped in-process
# caches (e.g. database._SLUG_CACHE) on every single click. If you edit
# config.py/paper_broker.py/database.py/live_broker.py/scanner.py while the
# dashboard is running, restart `streamlit run dashboard.py` to pick up changes.

st.set_page_config(
    page_title="Sureshot Terminal",
    page_icon=":material/bolt:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sureshot Terminal dark theme: Space Grotesk headings, IBM Plex Sans body,
# IBM Plex Mono numerics/status -- recreated from the UI/UX revamp design handoff
# using Streamlit's own widgets (metrics, tabs, dataframes, toggles) wherever they
# exist; custom HTML/CSS is used only for the things Streamlit has no primitive for
# (status pills, the nav underline, the read-only notice banner).
st.markdown("""
<style>
    /* ---------- Header bar (Custom HTML) ---------- */
    .sst-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
        padding: 16px 24px;
        margin: -1rem -1rem 22px -1rem;
        background: #0D1118;
        border-bottom: 1px solid #1B2330;
    }
    .sst-brand { display: flex; align-items: center; gap: 12px; }
    .sst-logo {
        width: 30px; height: 30px; border-radius: 8px; background: #4C8DE8;
        display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .sst-logo-mark { width: 10px; height: 10px; background: #0A0D12; transform: rotate(45deg); }
    .sst-brand-title { font-weight: 700; font-size: 17px; letter-spacing: 0.3px; color: #E7ECF3; }
    .sst-brand-sub { font-size: 11px; color: #7C8AA0; letter-spacing: 0.4px; }
    .sst-status-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .sst-status-pill {
        display: flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 20px;
        background: #111826; border: 1px solid #1E2733;
        font-size: 12px;
    }
    .sst-status-pill .dot { width: 7px; height: 7px; border-radius: 50%; }
    .sst-status-pill .lbl { color: #9BA8BC; margin-right: 2px; }
    .sst-status-pill .val { font-weight: 600; color: #E7ECF3; }
    .sst-status-pill.kill-active { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); }

    /* ---------- Status banner (Overview) ---------- */
    .sst-banner {
        display: flex; align-items: center; gap: 16px; padding: 18px 22px; border-radius: 12px;
        background: linear-gradient(90deg, #111826, #0F141D); border: 1px solid #1E2733;
        margin-bottom: 20px; flex-wrap: wrap;
    }
    .sst-banner-icon {
        width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .sst-banner-icon .core { width: 14px; height: 14px; border-radius: 50%; }
    .sst-banner-title { font-weight: 700; font-size: 18px; color: #E7ECF3; }
    .sst-banner-sub { font-size: 13px; color: #8B98AC; margin-top: 2px; }

    /* ---------- Read-only notice (Collaborator View) ---------- */
    .sst-readonly-notice {
        display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-radius: 8px;
        background: #111622; border: 1px dashed #2A3646; margin-bottom: 20px;
        font-size: 12px; color: #8B98AC;
    }
    .sst-readonly-notice .dot { width: 8px; height: 8px; border-radius: 50%; background: #5C6B82; flex-shrink: 0; }

    /* ---------- Section subtitle ---------- */
    .sst-section-sub { color: #7C8AA0; font-size: 0.85rem; margin-top: -8px; margin-bottom: 18px; }

    /* ---------- ENFORCED / result pills inside markdown ---------- */
    .sst-pill-enforced {
        font-size: 10px; font-weight: 600; padding: 3px 8px;
        border-radius: 10px; background: rgba(74, 222, 128, 0.15); color: #4ADE80;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_broker() -> PaperBroker:
    return PaperBroker()

@st.cache_data(ttl=10, show_spinner=False)
def get_db_trades(limit: int = 200, broker_filter: str = None, account_filter: str = None):
    return database.get_all_trades(limit=limit, broker_filter=broker_filter, account_filter=account_filter)


@st.cache_data(ttl=10, show_spinner=False)
def get_live_token_best_bid(token_id: str) -> Optional[float]:
    """Queries top bid price from live CLOB orderbook for an outcome token."""
    if not token_id:
        return None
    try:
        import polymarket_client
        client = polymarket_client.get_public_client()
        ob = client.get_order_book(token_id=str(token_id))
        if ob and ob.bids:
            return max(float(b.price) for b in ob.bids)
    except Exception:
        pass
    return None


@st.cache_data(ttl=15, show_spinner=False)
def fetch_on_chain_wallet_data(address: str):
    """Queries Polymarket's official Data API for an arbitrary wallet address.
    Returns (trades, positions, closed_pos, errors). Failures used to be swallowed by a
    bare print() -- invisible once this dashboard runs as a background NSSM/Windows
    service with no console -- so each failure is now also collected into `errors` for
    the caller to display with st.error(), instead of silently rendering as if the
    wallet just had no activity."""
    import polymarket_client
    client = polymarket_client.get_public_client()
    clean_addr = address.strip()
    trades = []
    positions = []
    closed_pos = []
    errors = []

    try:
        trades_paginator = client.list_trades(user=clean_addr, page_size=50)
        for t in trades_paginator.iter_items():
            p_val = float(t.price) if t.price is not None else 0.0
            s_val = float(t.size) if t.size is not None else 0.0
            t_slug = getattr(t, "slug", "") or ""
            t_eslug = getattr(t, "event_slug", "") or ""
            t_url = f"https://polymarket.com/market/{t_slug}" if t_slug else (f"https://polymarket.com/event/{t_eslug}" if t_eslug else "https://polymarket.com")
            trades.append({
                "Polymarket": t_url,
                "Timestamp": str(t.timestamp)[:19].replace("T", " ") if t.timestamp else "",
                "Market / Question": str(t.title or "")[:50],
                "Outcome": str(t.outcome or ""),
                "Side": str(t.side or "BUY"),
                "Tokens": round(s_val, 4),
                "Entry $": f"{p_val * 100:.2f}%" if p_val < 1.0 else f"${p_val:.2f}",
                "Cost $": f"${(s_val * p_val):.2f}",
                "Tx Hash": str(t.transaction_hash or "")[:12] + "..." if t.transaction_hash else "",
            })
    except Exception as e:
        # Fallback to direct Polymarket Data API
        try:
            import urllib.request, json
            url = f"https://data-api.polymarket.com/trades?user={clean_addr}&limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_trades = json.loads(resp.read().decode())
                for t in raw_trades:
                    p_val = float(t.get("price") or 0.0)
                    s_val = float(t.get("size") or 0.0)
                    t_slug = t.get("slug") or ""
                    t_eslug = t.get("eventSlug") or ""
                    t_url = f"https://polymarket.com/market/{t_slug}" if t_slug else (f"https://polymarket.com/event/{t_eslug}" if t_eslug else "https://polymarket.com")
                    raw_ts = t.get("timestamp")
                    ts_str = ""
                    if raw_ts:
                        try:
                            ts_str = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            ts_str = str(raw_ts)[:19].replace("T", " ")
                    trades.append({
                        "Polymarket": t_url,
                        "Timestamp": ts_str,
                        "Market / Question": str(t.get("title") or "")[:50],
                        "Outcome": str(t.get("outcome") or ""),
                        "Side": str(t.get("side") or "BUY").upper(),
                        "Tokens": round(s_val, 4),
                        "Entry $": f"{p_val * 100:.2f}%" if p_val < 1.0 else f"${p_val:.2f}",
                        "Cost $": f"${(s_val * p_val):.2f}",
                        "Tx Hash": str(t.get("transactionHash") or "")[:12] + "..." if t.get("transactionHash") else "",
                    })
        except Exception as fallback_err:
            err = f"Fetching trades failed: {type(fallback_err).__name__}: {fallback_err}"
            print(f"[dashboard] {err} (address={clean_addr})")
            errors.append(err)

    try:
        positions_paginator = client.list_positions(user=clean_addr)
        for p in positions_paginator.iter_items():
            avg_p = float(p.avg_price) if p.avg_price is not None else 0.0
            sz = float(p.size) if p.size is not None else 0.0
            c_pnl = float(p.cash_pnl) if p.cash_pnl is not None else 0.0
            p_slug = getattr(p, "slug", "") or ""
            p_eslug = getattr(p, "event_slug", "") or ""
            p_url = f"https://polymarket.com/market/{p_slug}" if p_slug else (f"https://polymarket.com/event/{p_eslug}" if p_eslug else "https://polymarket.com")
            positions.append({
                "Polymarket": p_url,
                "Market / Question": str(p.title or "")[:50],
                "Outcome": str(p.outcome or ""),
                "Tokens": round(sz, 4),
                "Avg Entry": f"{avg_p * 100:.2f}%" if avg_p < 1.0 else f"${avg_p:.2f}",
                "Cost $": f"${float(p.initial_value or 0):.2f}",
                "Current Value": f"${float(p.current_value or 0):.2f}",
                "Cash P&L": f"${c_pnl:+.2f}",
                "% P&L": f"{float(p.percent_pnl or 0):+.1f}%",
                "Redeemable": "✅ Yes" if p.redeemable else "No",
            })
    except Exception as e:
        # Fallback to direct Polymarket Data API
        try:
            import urllib.request, json
            url = f"https://data-api.polymarket.com/positions?user={clean_addr}&sizeThreshold=0.01&limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_pos = json.loads(resp.read().decode())
                for p in raw_pos:
                    avg_p = float(p.get("avgPrice") or 0.0)
                    sz = float(p.get("size") or 0.0)
                    c_pnl = float(p.get("cashPnl") or 0.0)
                    p_slug = p.get("slug") or ""
                    p_eslug = p.get("eventSlug") or ""
                    p_url = f"https://polymarket.com/market/{p_slug}" if p_slug else (f"https://polymarket.com/event/{p_eslug}" if p_eslug else "https://polymarket.com")
                    positions.append({
                        "Polymarket": p_url,
                        "Market / Question": str(p.get("title") or "")[:50],
                        "Outcome": str(p.get("outcome") or ""),
                        "Tokens": round(sz, 4),
                        "Avg Entry": f"{avg_p * 100:.2f}%" if avg_p < 1.0 else f"${avg_p:.2f}",
                        "Cost $": f"${float(p.get('initialValue') or 0):.2f}",
                        "Current Value": f"${float(p.get('currentValue') or 0):.2f}",
                        "Cash P&L": f"${c_pnl:+.2f}",
                        "% P&L": f"{float(p.get('percentPnl') or 0):+.1f}%",
                        "Redeemable": "✅ Yes" if p.get("redeemable") else "No",
                    })
        except Exception as fallback_err:
            err = f"Fetching open positions failed: {type(fallback_err).__name__}: {fallback_err}"
            print(f"[dashboard] {err} (address={clean_addr})")
            errors.append(err)

    try:
        if hasattr(client, "list_closed_positions"):
            closed_paginator = client.list_closed_positions(user=clean_addr)
            for cp in closed_paginator.iter_items():
                avg_p = float(cp.avg_price) if cp.avg_price is not None else 0.0
                cur_p = float(cp.cur_price) if cp.cur_price is not None else 0.0
                pnl_v = float(cp.realized_pnl) if cp.realized_pnl is not None else 0.0
                cp_slug = getattr(cp, "slug", "") or ""
                cp_eslug = getattr(cp, "event_slug", "") or ""
                cp_url = f"https://polymarket.com/market/{cp_slug}" if cp_slug else (f"https://polymarket.com/event/{cp_eslug}" if cp_eslug else "https://polymarket.com")
                raw_ts = getattr(cp, "timestamp", None)
                ts_str = ""
                if raw_ts:
                    try:
                        if isinstance(raw_ts, (int, float)):
                            ts_str = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            ts_str = str(raw_ts)[:19].replace("T", " ")
                    except Exception:
                        ts_str = str(raw_ts)[:19].replace("T", " ")
                closed_pos.append({
                    "Polymarket": cp_url,
                    "Market / Question": str(cp.title or "")[:50],
                    "Outcome": str(cp.outcome or ""),
                    "Avg Entry": f"{avg_p * 100:.2f}%" if avg_p < 1.0 else f"${avg_p:.2f}",
                    "Exit Price": f"{cur_p * 100:.2f}%" if cur_p < 1.0 else f"${cur_p:.2f}",
                    "Cost $": f"${float(cp.total_bought or 0):.2f}",
                    "Realized P&L": f"${pnl_v:+.2f}",
                    "Closed At": ts_str,
                })
        else:
            raise AttributeError("'PublicClient' object has no attribute 'list_closed_positions'")
    except Exception as e:
        # Fallback to direct Polymarket Data API
        try:
            import urllib.request, json
            url = f"https://data-api.polymarket.com/closed-positions?user={clean_addr}&limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_items = json.loads(resp.read().decode())
                for item in raw_items:
                    avg_p = float(item.get("avgPrice") or 0.0)
                    cur_p = float(item.get("curPrice") or 0.0)
                    pnl_v = float(item.get("realizedPnl") or 0.0)
                    total_bought = float(item.get("totalBought") or 0.0)
                    cp_slug = item.get("slug") or ""
                    cp_eslug = item.get("eventSlug") or ""
                    cp_url = f"https://polymarket.com/market/{cp_slug}" if cp_slug else (f"https://polymarket.com/event/{cp_eslug}" if cp_eslug else "https://polymarket.com")
                    raw_ts = item.get("timestamp")
                    ts_str = ""
                    if raw_ts:
                        try:
                            ts_str = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            ts_str = str(raw_ts)[:19].replace("T", " ")
                    closed_pos.append({
                        "Polymarket": cp_url,
                        "Market / Question": str(item.get("title") or "")[:50],
                        "Outcome": str(item.get("outcome") or ""),
                        "Avg Entry": f"{avg_p * 100:.2f}%" if avg_p < 1.0 else f"${avg_p:.2f}",
                        "Exit Price": f"{cur_p * 100:.2f}%" if cur_p < 1.0 else f"${cur_p:.2f}",
                        "Cost $": f"${total_bought:.2f}",
                        "Realized P&L": f"${pnl_v:+.2f}",
                        "Closed At": ts_str,
                    })
        except Exception as fallback_err:
            err = f"Fetching closed positions failed: {type(fallback_err).__name__}: {fallback_err}"
            print(f"[dashboard] {err} (address={clean_addr})")
            errors.append(err)

    return trades, positions, closed_pos, errors


def render_pnl_bar_chart(trades_for_chart: list, height: int = 160):
    """Renders the 'Realized P&L, Last 10 Trades' bar chart (green wins / red losses)
    using Altair so per-bar coloring stays a native chart, not raw HTML."""
    # get_all_trades() returns newest-first (ORDER BY placed_at DESC), so the last 10
    # are the HEAD of the list -- [-10:] was rendering the ten oldest trades under a
    # "Last 10" label. Reverse the slice so the chart reads left-to-right in time.
    closed = [
        t for t in trades_for_chart
        if "PENDING" not in str(t.get("result", "")).upper()
        and str(t.get("result", "")).upper() != "VOID"
    ]
    last10 = list(reversed(closed[:10]))
    if not last10:
        st.info("No settled trades yet to chart.")
        return
    rows = []
    for i, t in enumerate(last10):
        pnl_val = float(t.get("pnl") or 0.0)
        rows.append({
            "idx": i + 1,
            "pnl": pnl_val,
            "outcome": "Win" if database.classify_result(pnl_val) == "WON" else "Loss",
            "question": str(t.get("question", ""))[:40],
        })
    df = pd.DataFrame(rows)
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("idx:O", axis=None),
            y=alt.Y("pnl:Q", axis=alt.Axis(title=None, grid=False)),
            color=alt.Color(
                "outcome:N",
                scale=alt.Scale(domain=["Win", "Loss"], range=["#4ADE80", "#F87171"]),
                legend=None,
            ),
            tooltip=["question", alt.Tooltip("pnl:Q", format="+.2f")],
        )
        .properties(height=height, background="transparent")
        .configure_view(strokeWidth=0)
    )
    st.altair_chart(chart, width="stretch")


# ---------------------------------------------------------------------------
# Cached read-only wrappers for expensive/network-bound calls.
#
# Streamlit executes the body of every st.tabs()/st.expander() block on every
# script rerun regardless of which one is visually open -- so without caching,
# a single click anywhere in the app re-fetches on-chain vitals/orders/positions
# for every configured live account, plus re-queries the trades DB multiple
# times, on every interaction. A short TTL keeps the dashboard responsive
# without showing meaningfully stale data.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=8, show_spinner=False)
def _cached_all_trades(broker_filter=None, account_filter=None):
    return get_db_trades(broker_filter=broker_filter, account_filter=account_filter)


@st.cache_data(ttl=8, show_spinner=False)
def _cached_live_aggregated_vitals():
    inst = live_broker.get_live_broker()
    return inst.get_aggregated_vitals() if inst else None


@st.cache_data(ttl=8, show_spinner=False)
def _cached_live_open_orders(account_name=None):
    inst = live_broker.get_live_broker()
    return inst.get_open_orders(account_name=account_name) if inst else []


@st.cache_data(ttl=8, show_spinner=False)
def _cached_live_positions(account_name=None):
    inst = live_broker.get_live_broker()
    return inst.get_live_positions(account_name=account_name) if inst else []


@st.cache_data(ttl=8, show_spinner=False)
def _cached_live_collateral_balance(account_name=None):
    inst = live_broker.get_live_broker()
    return inst.get_collateral_balance(account_name=account_name) if inst else 0.0


@st.cache_data(ttl=8, show_spinner=False)
def _cached_account_vitals(account_name):
    inst = live_broker.get_live_broker()
    if not inst:
        return None
    session = inst.get_session(account_name)
    return session.get_account_vitals() if session else None


# ---------------------------------------------------------------------------
# Shared exit handler.
#
# The Overview and History screens each had their own near-identical copy of this
# logic, and both shared the same two defects: they treated "the sell call didn't
# raise" as "the position is sold", and their fallback path wrote only to
# trades.db -- leaving the state.json position in place, so an exited position
# kept reappearing under Open Positions no matter how many times you exited it.
# One implementation now, and every branch clears BOTH stores or clears neither.
# ---------------------------------------------------------------------------

def render_open_orders(orders, account_name=None, key_prefix="orders"):
    """Renders resting CLOB orders with per-order and bulk cancel controls.

    Unfilled limit orders used to sit in the book indefinitely with nothing in the
    app able to clear them -- and, because orders are no longer booked as positions
    unless they fill, this is now the only place a working order is visible.
    """
    st.dataframe(pd.DataFrame(orders), hide_index=True)
    st.caption(
        "These orders are resting on the book and are **not** tracked as positions. "
        "They can still fill later, at a price the strategy may no longer want."
    )
    cancel_cols = st.columns([1.4, 1])
    with cancel_cols[0]:
        order_labels = {
            f"{o.get('account')} · {o.get('side')} {float(o.get('size', 0)):.2f} @ ${float(o.get('price', 0)):.4f} · {str(o.get('id'))[:14]}": o
            for o in orders
        }
        chosen = st.selectbox("Order to cancel", list(order_labels.keys()), key=f"{key_prefix}_cancel_pick")
    with cancel_cols[1]:
        st.write("")
        if st.button("Cancel Order", icon=":material/cancel:", key=f"{key_prefix}_cancel_one", width="stretch"):
            target = order_labels[chosen]
            inst = live_broker.get_live_broker()
            try:
                inst.cancel_order(str(target.get("id")), account_name=target.get("account"))
                _cached_live_open_orders.clear()
                st.success("Order cancelled.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not cancel order: {exc}")

    confirm_key = f"{key_prefix}_confirm_cancel_all"
    if st.session_state.get(confirm_key):
        if st.button("Confirm: cancel ALL resting orders", type="primary", key=f"{key_prefix}_cancel_all_go", width="stretch"):
            inst = live_broker.get_live_broker()
            results = inst.cancel_all_orders(account_name)
            st.session_state.pop(confirm_key, None)
            _cached_live_open_orders.clear()
            failed = [r for r in results if not r.get("success")]
            if failed:
                st.error("Some accounts failed: " + "; ".join(f"{r['account']}: {r.get('error')}" for r in failed))
            else:
                st.success("Cancelled all resting orders.")
            st.rerun()
    else:
        if st.button("Cancel All Resting Orders", icon=":material/delete_sweep:", key=f"{key_prefix}_cancel_all", width="stretch"):
            st.session_state[confirm_key] = True
            st.rerun()


def is_settled_trade(trade) -> bool:
    """A row that represents a real, completed trade.

    VOID rows were recorded locally but never executed on Polymarket, so they are
    neither open nor a win/loss -- counting them as settled would put a zero-P&L
    non-event into the win-rate denominator.
    """
    result = str(trade.get("result", "")).upper()
    return "PENDING" not in result and result != "VOID"


def execute_exit(position_key, position, exit_price, order_type="LIMIT", key_prefix=""):
    """Exits one position. Returns True when the caller should st.rerun()."""
    mode = str(position.get("mode", execution_mode_str)).upper()
    trade_id = position.get("trade_id")
    token_id = str(position.get("token_id", position_key))
    account_name = position.get("account_name", config.DEFAULT_ACCOUNT_NAME)
    shares_held = float(position.get("shares") or 0.0)
    invested = float(position.get("stake", 0.0))

    def _settle_locally(price, note):
        """Books the exit in state.json and trades.db together."""
        booked = broker.exit_position(position_key, exit_price=price, note=note)
        if booked is None and trade_id:
            # No matching state.json position (already gone): settle the DB row alone.
            database.exit_orphaned_trade(trade_id, exit_price=price, note=note)
        elif trade_id:
            database.exit_orphaned_trade(trade_id, exit_price=price, note=note)
        _cached_all_trades.clear()

    def _clear_phantom(note):
        """Drops a position that isn't real, from both stores."""
        broker.discard_position(trade_id or token_id, note=note)
        if trade_id:
            database.void_trade(trade_id, note=note)
        _cached_all_trades.clear()

    if mode != "LIVE":
        note = f"Manual Paper Exit @ ${exit_price:.4f}"
        _settle_locally(exit_price, note)
        st.success(f"Closed paper trade at ${exit_price:.4f}.")
        return True

    live_inst = live_broker.get_live_broker()
    if not live_inst:
        st.error("Live broker not ready. Check credentials in Control and Risk.")
        return False

    try:
        with st.spinner(f"Submitting sell order on CLOB for {account_name}..."):
            outcome = live_inst.exit_position(account_name, token_id, size=shares_held, price=exit_price, order_type=order_type)
    except Exception as ex:
        if live_broker.is_no_balance_rejection(None, str(ex)):
            return _reconcile_after_failed_exit(live_inst, account_name, trade_id, token_id, _clear_phantom)
        st.error(f"Failed to exit on-chain: {ex}")
        return False

    # Only an actual fill closes the position locally.
    if outcome.get("filled") and float(outcome.get("filled_size", 0.0)) > 0:
        fill_price = float(outcome.get("avg_price") or exit_price)
        filled_size = float(outcome["filled_size"])
        realized = filled_size * fill_price
        note = f"Manual Live Exit @ ${fill_price:.4f}"
        if filled_size + 1e-9 < shares_held:
            st.warning(
                f"Partial fill: {filled_size:.2f} of {shares_held:.2f} shares sold at "
                f"${fill_price:.4f}. Booking the full position at the realized price; "
                f"run Sync to correct the remainder against on-chain balances."
            )
        _settle_locally(fill_price, note)
        st.success(f"Sold {filled_size:.2f} shares at ${fill_price:.4f} (${realized:.2f}). Position closed.")
        return True

    if outcome.get("resting"):
        st.warning(
            f"Sell order accepted but resting unfilled on the book "
            f"(order {outcome.get('order_id')}). The position is still open and has "
            f"NOT been closed locally. Lower the exit price to cross the spread, or "
            f"cancel the order from Control and Risk."
        )
        return False

    code = str(outcome.get("code") or "")
    if live_broker.is_no_balance_rejection(code, outcome.get("message", "")):
        return _reconcile_after_failed_exit(live_inst, account_name, trade_id, token_id, _clear_phantom)

    st.error(f"Exchange rejected the sell order ({code or 'unknown'}): {outcome.get('message')}")
    return False


def _reconcile_after_failed_exit(live_inst, account_name, trade_id, token_id, clear_phantom):
    """The wallet holds none of this token. Ask Polymarket what actually happened
    instead of assuming an outcome."""
    with st.spinner("No on-chain balance for this token. Checking Polymarket records..."):
        reconciled = live_inst.reconcile_positions(account_name)
    _cached_all_trades.clear()
    if reconciled:
        first = reconciled[0]
        if first.get("voided"):
            st.warning(f"This position was never actually filled on Polymarket. Voided: {first.get('note')}")
        else:
            st.success(f"Reconciled against Polymarket: {first.get('note')} (P&L ${first.get('pnl', 0.0):+.2f})")
        return True

    # Reconciliation could not establish an outcome. The wallet holds nothing, so
    # this row cannot be an open position -- void it in BOTH stores rather than
    # settling it as a loss (the old behaviour) or leaving it stuck in state.json.
    clear_phantom("Voided: no on-chain balance and no matching Polymarket trade")
    st.warning(
        "No on-chain balance and no matching Polymarket trade for this position, so "
        "it was never actually executed. Removed from tracking as VOID (no P&L booked)."
    )
    return True


broker = get_broker()
settings = settings_manager.load_settings()
state = broker.state

# Current Execution Mode
is_live = bool(settings.get("live_trading", False))
execution_mode_str = "LIVE" if is_live else "PAPER"

# state.json holds LIVE and PAPER positions/trades together (distinguished only by each
# entry's "mode" field), so summary() must be scoped to the active mode -- otherwise the
# Overview KPI ribbon silently blends both regardless of which mode is selected.
summary = broker.summary(mode_filter=execution_mode_str)
kill_switch_active = bool(settings.get("entry_kill_switch", False))
creds_ok, creds_msg = live_broker.check_credentials_available()
status = settings.get("bot_status", "RUNNING")


# ==========================================
# SIDEBAR: PRIMARY STRATEGY CONTROLS
# ==========================================
with st.sidebar:
    st.markdown(
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">'
        '<span style="font-size:1.3rem;">🎛️</span>'
        '<span style="font-family:\'Space Grotesk\',sans-serif;font-weight:700;font-size:1.1rem;color:#E7ECF3;">Control Panel</span>'
        '</div>'
        '<div style="color:#7C8AA0;font-size:0.78rem;margin-bottom:14px;">Runtime strategy &amp; risk controls</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown('<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;color:#7C8AA0;margin-bottom:8px;">⚡ Quick Actions</div>', unsafe_allow_html=True)
        col_sb_status, col_sb_mode = st.columns(2)
        with col_sb_status:
            if status == "RUNNING":
                st.badge("RUNNING", icon=":material/play_arrow:", color="green")
            else:
                st.badge("PAUSED", icon=":material/pause:", color="orange")
        with col_sb_mode:
            if is_live:
                st.badge("LIVE", icon=":material/wifi:", color="blue")
            else:
                st.badge("PAPER", icon=":material/description:", color="gray")

        st.write("")
        if status == "RUNNING":
            if st.button("Pause", icon=":material/pause:", width="stretch"):
                settings_manager.update_setting("bot_status", "PAUSED")
                st.rerun()
        else:
            if st.button("Resume", icon=":material/play_arrow:", width="stretch", type="primary"):
                settings_manager.update_setting("bot_status", "RUNNING")
                st.rerun()

        st.write("")
        if st.button("🚨 PANIC KILL-SWITCH", help="Immediately stops opening any new positions", width="stretch"):
            settings_manager.update_setting("entry_kill_switch", True)
            st.error("PANIC KILL-SWITCH ACTIVATED! New orders blocked.")
            st.rerun()

    st.write("")

    # --- Settings Form: grouped into tabs so related controls are one click away
    # instead of one long scroll, while still saving together as a single config. ---
    with st.form("sidebar_config_form"):
        st.markdown('<div style="font-family:\'Space Grotesk\',sans-serif;font-weight:700;font-size:0.95rem;color:#E7ECF3;margin-bottom:2px;">⚙️ Strategy Configuration</div>', unsafe_allow_html=True)
        sb_tab_general, sb_tab_gates, sb_tab_risk, sb_tab_wallet = st.tabs(["General", "Gates", "Risk", "Wallet"])

        with sb_tab_general:
            poll_interval = st.number_input(
                "Cooldown (seconds)",
                min_value=10,
                max_value=600,
                value=int(settings.get("poll_interval_seconds", 60)),
                step=5,
                help="Polling interval cooldown between market scans.",
            )
            max_signals = st.number_input(
                "Max Signals Per Scan",
                min_value=1,
                max_value=25,
                value=int(settings.get("max_signals_per_scan", 5)),
                step=1,
            )
            only_sports = st.checkbox(
                "Focus Exclusively on Sports",
                value=bool(settings.get("only_sports", True)),
            )
            only_moneyline = st.checkbox(
                "Moneyline Matches Only",
                value="moneyline" in settings.get("sports_market_types", ["moneyline"]),
            )

        with sb_tab_gates:
            st.markdown("**Health & Confidence**")
            req_healthy = st.checkbox(
                "Require Healthy Data",
                value=bool(settings.get("require_healthy_data", True)),
            )
            req_high_conf = st.checkbox(
                "Require High Confidence Match",
                value=bool(settings.get("require_high_confidence", False)),
            )
            st.markdown("**Late Game**")
            late_game_enabled = st.checkbox(
                "Late Game Enabled",
                value=bool(settings.get("late_game_enabled", False)),
                help="Scan only matches the server reports as live, and enter only "
                     "when that sport's own in-play state says little real time is "
                     "left. end_date is never used to judge this.",
            )
            late_game_max_minutes = st.number_input(
                "Max Remaining Game Time (minutes)",
                min_value=1.0,
                max_value=240.0,
                value=float(settings.get("late_game_max_remaining_minutes", 30.0)),
                step=1.0,
                help="Absolute cap on estimated wall-clock minutes left in the match.",
            )
            late_game_fraction = st.slider(
                "...or this share of the format's full length",
                min_value=0.0,
                max_value=1.0,
                value=float(settings.get("late_game_max_remaining_fraction", 0.34)),
                step=0.01,
                help="The tighter of the two caps applies, so 'late game' means the "
                     "same share of a 20-minute esports map as of a 210-minute "
                     "football game. Set to 0 to use the minute cap alone.",
            )
            late_game_min_prob = st.slider(
                "Min Entry Probability",
                min_value=0.50,
                max_value=0.999,
                value=float(settings.get("late_game_min_probability", 0.90)),
                step=0.005,
                format="%.3f",
                help="Floor for the exact outcome token being bought. While Late "
                     "Game is on this band replaces Min/Max Price entirely.",
            )
            late_game_max_prob = st.slider(
                "Max Entry Probability",
                min_value=0.50,
                max_value=0.999,
                value=float(settings.get("late_game_max_probability", 0.99)),
                step=0.005,
                format="%.3f",
            )
            late_game_require_clock = st.checkbox(
                "Only trade sports with a real game clock",
                value=bool(settings.get("late_game_require_clock", False)),
                help="Strictest timing rule: trade only where an in-play clock is "
                     "published (soccer). Esports, timed by counting remaining maps "
                     "rather than reading a clock, is skipped too.",
            )
            late_game_worst_case = st.checkbox(
                "Estimate sports with no in-period clock",
                value=bool(settings.get("late_game_allow_worst_case_periods", False)),
                help="NBA quarters and NHL periods publish no clock, so these sports "
                     "are skipped by default. Turning this on assumes the whole "
                     "current period remains -- never enters early, but the estimate "
                     "is wide enough that those sports need a raised minute cap "
                     "before they can qualify.",
            )
            late_game_rules_text = st.text_area(
                "Per-sport overrides (JSON)",
                value=json.dumps(settings.get("late_game_sport_rules", {}) or {}, indent=2),
                height=120,
                help='Keyed by sport code, e.g. {"nhl": {"max_remaining_minutes": 45}}. '
                     'Keys: enabled, allow_worst_case, max_remaining_minutes, '
                     'max_remaining_fraction.',
            )

        with sb_tab_risk:
            st.markdown("**Probability & Odds**")
            # There is one entry band. While Late Game is on it lives in the Gates tab
            # and these two are read by nothing, so say so and disable them rather than
            # leaving controls that look live and change nothing.
            band_is_late_game = bool(settings.get("late_game_enabled", False))
            if band_is_late_game:
                lg_low, lg_high = settings_manager.effective_price_band(settings)
                st.caption(
                    f"Late Game is on, so the entry band is Min/Max Entry Probability "
                    f"on the **Gates** tab - currently **{lg_low:.3f} - {lg_high:.3f}**. "
                    f"The two sliders below do not apply in this mode."
                )
            price_min = st.slider(
                "Min Price (Entry Floor)",
                min_value=0.85,
                max_value=0.995,
                value=float(settings.get("price_min", 0.97)),
                step=0.005,
                format="%.3f",
                disabled=band_is_late_game,
                help="Used by the standard scan. While Late Game is on, the band comes "
                     "from Min/Max Entry Probability on the Gates tab instead.",
            )
            price_max = st.slider(
                "Max Price (Entry Ceiling)",
                min_value=0.95,
                max_value=0.999,
                value=float(settings.get("price_max", 0.995)),
                step=0.001,
                format="%.3f",
                disabled=band_is_late_game,
            )
            min_volume = st.number_input(
                "Min 24h Volume ($)",
                min_value=0.0,
                max_value=100000.0,
                value=float(settings.get("min_volume", 5000.0)),
                step=500.0,
            )
            min_liquidity = st.number_input(
                "Min Book Liquidity ($)",
                min_value=0.0,
                max_value=50000.0,
                value=float(settings.get("min_liquidity", 1000.0)),
                step=250.0,
            )
            st.markdown("**Position Sizing & Caps**")
            stake_per_trade = st.number_input(
                "Stake Per Trade ($)",
                min_value=1.0,
                max_value=1000.0,
                value=float(settings.get("stake_per_trade", 25.0)),
                step=5.0,
            )
            max_positions = st.number_input(
                "Max Open Positions",
                min_value=1,
                max_value=50,
                value=int(settings.get("max_open_positions", 10)),
                step=1,
            )
            max_exposure = st.number_input(
                "Max Total Exposure ($)",
                min_value=10.0,
                max_value=5000.0,
                value=float(settings.get("max_total_exposure", 200.0)),
                step=25.0,
            )
            max_daily_trades = st.number_input(
                "Max Trades Per Day",
                min_value=1,
                max_value=100,
                value=int(settings.get("max_trades_per_day", 10)),
                step=1,
            )

        with sb_tab_wallet:
            st.markdown("**👛 Wallet Tracking (Data API)**")
            tracked_wallet = st.text_input(
                "Polymarket / Proxy Address",
                value=str(settings.get("tracked_wallet_address", "")),
                placeholder="0x...",
                help="Enter any Polymarket profile or proxy wallet address to track on-chain activity, trades, and PnL.",
            )

        st.write("")
        saved = st.form_submit_button("💾 Save & Apply Config", width="stretch", type="primary")
        if saved:
            updated_settings = {
                "poll_interval_seconds": poll_interval,
                "require_healthy_data": req_healthy,
                "require_high_confidence": req_high_conf,
                "late_game_enabled": late_game_enabled,
                "late_game_max_remaining_minutes": float(late_game_max_minutes),
                "late_game_max_remaining_fraction": float(late_game_fraction),
                "late_game_min_probability": float(late_game_min_prob),
                "late_game_max_probability": float(late_game_max_prob),
                "late_game_allow_worst_case_periods": late_game_worst_case,
                "late_game_require_clock": late_game_require_clock,
                "late_game_sport_rules": _parse_sport_rules(
                    late_game_rules_text, settings.get("late_game_sport_rules", {})),
                "tracked_wallet_address": tracked_wallet.strip(),
                "price_min": price_min,
                "price_max": price_max,
                "min_volume": min_volume,
                "min_liquidity": min_liquidity,
                "stake_per_trade": stake_per_trade,
                "max_open_positions": max_positions,
                "max_total_exposure": max_exposure,
                "max_trades_per_day": max_daily_trades,
                "max_signals_per_scan": max_signals,
                "only_sports": only_sports,
                "sports_market_types": ["moneyline"] if only_moneyline else [],
            }
            settings.update(updated_settings)
            settings_manager.save_settings(settings)
            st.success("Configuration saved and applied!")
            st.rerun()


# ==========================================
# HEADER BAR: brand + live status pills (STATUS / MODE / KILL SWITCH)
# ==========================================
_mode_color = "#4C8DE8" if is_live else "#5C6B82"
if kill_switch_active:
    _kill_pill_cls, _kill_dot, _kill_label = "kill-active", "#F87171", "ACTIVE"
else:
    _kill_pill_cls, _kill_dot, _kill_label = "", "#4ADE80", "ARMED"
_status_dot = "#4ADE80" if status == "RUNNING" else "#FBBF24"

_header_html = f"""
<div class="sst-header">
  <div class="sst-brand">
    <div class="sst-logo"><div class="sst-logo-mark"></div></div>
    <div>
      <div class="sst-brand-title">SURESHOT TERMINAL</div>
      <div class="sst-brand-sub">SPORTS MONEYLINE EXECUTION ENGINE</div>
    </div>
  </div>
  <div class="sst-status-row">
    <div class="sst-status-pill"><div class="dot" style="background:{_status_dot};"></div><span class="lbl">STATUS</span><span class="val">{status}</span></div>
    <div class="sst-status-pill"><div class="dot" style="background:{_mode_color};"></div><span class="lbl">MODE</span><span class="val" style="color:{_mode_color};">{execution_mode_str}</span></div>
    <div class="sst-status-pill {_kill_pill_cls}"><div class="dot" style="background:{_kill_dot};"></div><span class="lbl">KILL SWITCH</span><span class="val">{_kill_label}</span></div>
  </div>
</div>
"""
st.markdown(_header_html, unsafe_allow_html=True)


# ==========================================
# NAV: 4 consolidated screens
# (Execution + Signals + health summary -> Overview;
#  Live Control + risk-gate table -> Control and Risk;
#  Trade History + Performance Analytics -> History and Performance;
#  new read-only screen -> Collaborator View)
# ==========================================
tab_overview, tab_control, tab_history, tab_collab = st.tabs([
    "OVERVIEW",
    "CONTROL AND RISK",
    "HISTORY AND PERFORMANCE",
    "COLLABORATOR VIEW",
])



def build_gates_data():
    band_low, band_high = settings_manager.effective_price_band(settings)
    p_floor, p_ceil = band_low * 100, band_high * 100
    daily_limit = int(settings.get("max_trades_per_day", 10))
    exposure_limit = float(settings.get("max_total_exposure", 200.0))
    cooldown = int(settings.get("poll_interval_seconds", 60))
    slippage = float(settings.get("max_slippage", 0.005) or 0.0)
    late_game_on = bool(settings.get("late_game_enabled", False))
    min_hours = float(settings.get("min_hours_to_resolution", 1.0))
    late_minutes = float(settings.get("late_game_max_remaining_minutes", 30.0))
    late_fraction = float(settings.get("late_game_max_remaining_fraction", 0.34))
    return [
        {"Rule": "Probability Floor Threshold", "Value": f"= {p_floor:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Probability Ceiling Threshold", "Value": f"= {p_ceil:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Daily Trades Limit", "Value": f"≤ {daily_limit}", "Status": "ENFORCED"},
        {"Rule": "Max Total Exposure", "Value": f"≤ ${exposure_limit:,.2f}", "Status": "ENFORCED"},
        {"Rule": "Max Entry Slippage", "Value": f"≤ ${slippage:.4f} vs quote" if slippage > 0 else "OFF", "Status": "ENFORCED" if slippage > 0 else "DISABLED"},
        {"Rule": "Fill Verification", "Value": "Positions booked on fill only", "Status": "ENFORCED"},
        # Late Game replaces the resolution-window floor rather than stacking with it,
        # so report which one is actually in force instead of always showing min_hours.
        {
            "Rule": "Entry Timing Gate",
            "Value": (f"Live game: ≤ {late_minutes:g} min left"
                      + (f" or {late_fraction:.0%} of the format" if late_fraction > 0 else "")
                      ) if late_game_on else f"≥ {min_hours:g}h to resolve",
            "Status": "ENFORCED",
        },
        {"Rule": "Cooldown Timer / Loop Interval", "Value": f"{cooldown}s", "Status": "ENFORCED"},
        {"Rule": "Entry Kill Switch", "Value": "ACTIVE" if kill_switch_active else "ARMED", "Status": "ENFORCED"},
        {"Rule": "Sports Moneyline Filter", "Value": "Tag 100639 / ML", "Status": "ENFORCED"},
    ]


# =============================================================
# SCREEN 1: OVERVIEW
# "Is it safe, is it making money" in one glance.
# =============================================================
with tab_overview:
    lifecycle = summary.get("order_lifecycle", {})
    reserved_capital = summary.get("reserved_capital", summary.get("open_exposure", 0.0))
    # Merge positions from state.json and pending trades from trades.db so nothing is missed
    all_known_positions = dict(state.get("positions", {}))
    try:
        # Reconciliation is NOT run here. It performs on-chain writes to the local
        # books (settling and voiding trades), and running it on every Streamlit
        # rerun meant it fired on page load and on every widget interaction. It is
        # now only triggered explicitly, by the Sync and Refresh buttons.
        db_pending = [t for t in get_db_trades(limit=200) if str(t.get("result", "")).upper() == "PENDING"]
        existing_trade_ids = {str(p.get("trade_id")) for p in all_known_positions.values() if p.get("trade_id")}
        for pt in db_pending:
            pt_tok = str(pt.get("token_id", ""))
            tr_id = str(pt.get("trade_id", ""))
            if pt_tok and tr_id and tr_id not in existing_trade_ids:
                pt_acc = str(pt.get("account_name", "acc"))
                pos_k = f"{pt_acc}_{pt_tok}_{int(time.time()*1000)}"
                all_known_positions[pos_k] = {
                    "trade_id": pt.get("trade_id"),
                    "mode": str(pt.get("broker", pt.get("mode", "paper"))).upper(),
                    "account_name": pt.get("account_name", config.DEFAULT_ACCOUNT_NAME),
                    "wallet_address": pt.get("wallet_address", ""),
                    "token_id": pt_tok,
                    "market_id": pt.get("market_id"),
                    "slug": pt.get("slug", ""),
                    "question": pt.get("question", ""),
                    "outcome_label": pt.get("outcome_label") or pt.get("outcome", ""),
                    "entry_price": float(pt.get("entry_price", 0.0) or 0.0),
                    "shares": float(pt.get("tokens", pt.get("shares", 0.0)) or 0.0),
                    "stake": float(pt.get("cost", pt.get("stake", 0.0)) or 0.0),
                    "time_left": str(pt.get("time_left", "0.0m")),
                }
                existing_trade_ids.add(tr_id)
    except Exception:
        pass

    live_count = len([p for p in all_known_positions.values() if str(p.get("mode", "")).upper() == "LIVE"])
    paper_count = len([p for p in all_known_positions.values() if str(p.get("mode", "")).upper() == "PAPER"])
    positions = {
        k: p for k, p in all_known_positions.items()
        if str(p.get("mode", "PAPER")).upper() == execution_mode_str.upper()
    }
    signals = state.get("signals", [])

    # --- Status banner ---
    is_healthy = (status == "RUNNING") and not kill_switch_active
    if is_healthy:
        icon_bg, core_color, banner_title = "rgba(74,222,128,0.15)", "#4ADE80", f"System Healthy, {execution_mode_str} Mode"
        banner_sub = "All circuit breakers enforced."
    elif kill_switch_active:
        icon_bg, core_color, banner_title = "rgba(248,113,113,0.15)", "#F87171", f"Kill Switch Active, {execution_mode_str} Mode"
        banner_sub = "New entries are blocked. Existing positions still tracked to resolution."
    else:
        icon_bg, core_color, banner_title = "rgba(251,191,36,0.15)", "#FBBF24", f"Bot Paused, {execution_mode_str} Mode"
        banner_sub = "No new scans or entries will run until resumed."

    banner_col1, banner_col2 = st.columns([5, 1])
    with banner_col1:
        st.markdown(
            f"""
            <div class="sst-banner">
              <div class="sst-banner-icon" style="background:{icon_bg};"><div class="core" style="background:{core_color};"></div></div>
              <div style="flex:1;min-width:200px;">
                <div class="sst-banner-title">{banner_title}</div>
                <div class="sst-banner-sub">{banner_sub}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with banner_col2:
        st.write("")
        if st.button("SCAN NOW", icon=":material/radar:", type="primary", width="stretch", key="overview_scan_now"):
            settings_manager.update_setting("manual_scan_requested", True)
            with st.spinner("Scanning Polymarket sports markets..."):
                opps = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
                rejections = scanner.rejection_summary()
                broker.save_signals(opps)
                broker.add_log(f"Manual scan completed: {len(opps)} opportunities found."
                               + (f" Rejected: {rejections}." if rejections else ""))
            # A failed scan used to be indistinguishable from an empty one.
            if scanner.LAST_SCAN_ERROR:
                st.error(f"Scan failed: {scanner.LAST_SCAN_ERROR}. Results below may be incomplete.")
            else:
                st.success(f"Scan complete! Found {len(opps)} signals.")
            # An empty scan is otherwise unexplainable from the UI: say whether nothing
            # was live, everything was too early, or the prices were simply out of band.
            if not opps and rejections:
                st.session_state["last_scan_rejections"] = rejections
            st.rerun()

    last_rejections = st.session_state.pop("last_scan_rejections", None)
    if last_rejections:
        pretty = ", ".join(f"{reason.replace('_', ' ')}: {count}"
                           for reason, count in sorted(last_rejections.items(),
                                                       key=lambda kv: -kv[1]))
        st.info(f"No signals. Candidates were rejected for - {pretty}.")

    # --- Metrics row ---
    with st.container(horizontal=True):
        st.metric("Net PnL", f"${summary.get('realized_pnl', 0.0):+,.2f}", f"{summary.get('closed_trades', 0)} settled trades", border=True)
        st.metric("Win Rate", f"{summary.get('win_rate', 0.0):.1f}%", f"{summary.get('wins', 0)}W / {summary.get('losses', 0)}L", border=True)
        st.metric("Open Exposure", f"${summary.get('open_exposure', 0.0):,.2f}", f"{summary.get('open_positions', 0)} of {settings.get('max_open_positions', 10)} positions", border=True)
        st.metric("Reserved Capital", f"${reserved_capital:,.2f}", border=True)

    st.write("")
    col_positions, col_gates = st.columns([2.4, 1])

    with col_positions:
        col_pos_title, col_pos_filter, col_pos_sync = st.columns([1.1, 1.2, 0.7])
        with col_pos_title:
            st.markdown("##### Open Positions")
        with col_pos_filter:
            pos_filter_opts = [f"Active ({execution_mode_str.upper()})", f"All ({len(all_known_positions)})"]
            selected_pos_view = st.segmented_control("Filter Positions", pos_filter_opts, default=pos_filter_opts[0], key="ov_pos_filter_choice", label_visibility="collapsed") or pos_filter_opts[0]
        with col_pos_sync:
            if st.button("Sync", icon=":material/sync:", help="Sync active positions with on-chain Polymarket trades & fills", key="ov_sync_onchain", width="stretch"):
                reconciled_list = []
                live_inst = live_broker.get_live_broker()
                if live_inst:
                    with st.spinner("Checking Polymarket on-chain records..."):
                        reconciled_list = live_inst.reconcile_positions()
                _cached_all_trades.clear()
                if reconciled_list:
                    st.success(f"Synced {len(reconciled_list)} trade(s) from Polymarket!")
                else:
                    st.info("Positions are fully in sync with Polymarket.")
                st.rerun()

        if "All" in selected_pos_view:
            positions = all_known_positions
        else:
            positions = {
                k: p for k, p in all_known_positions.items()
                if str(p.get("mode", "PAPER")).upper() == execution_mode_str.upper()
            }

        if not positions:
            other_mode = "PAPER" if execution_mode_str.upper() == "LIVE" else "LIVE"
            other_count = paper_count if execution_mode_str.upper() == "LIVE" else live_count
            if other_count > 0:
                st.info(f"No active **{execution_mode_str.upper()}** positions currently tracked. You have **{other_count} {other_mode}** positions active — select **All ({len(all_known_positions)})** above to view and exit them.")
            else:
                st.info("No active positions currently tracked.")
        else:
            df_pos = []
            for tid, p in positions.items():
                mode = p.get("mode", execution_mode_str)
                slug_val = p.get("slug") or database.resolve_market_slug(p.get("market_id"))
                poly_url = database.get_polymarket_url(slug_val, p.get("market_id"))
                df_pos.append({
                    "Verify Trade": poly_url,
                    "Account": p.get("account_name", config.DEFAULT_ACCOUNT_NAME),
                    "Mode": mode,
                    "Question": p.get("question", "")[:50],
                    "Outcome": p.get("outcome_label", ""),
                    "Avg Entry": f"${p.get('entry_price', 0):.4f}",
                    "Capital": f"${p.get('stake', 0):.2f}",
                    "Time Left": p.get("time_left", "0.0m"),
                })
            st.dataframe(
                pd.DataFrame(df_pos),
                column_config={
                    "Verify Trade": st.column_config.LinkColumn("Verify Trade", display_text="View ↗"),
                },
                hide_index=True,
            )
            # Positions whose outcome could not be confirmed are now left PENDING and
            # flagged, rather than being auto-settled as wins.
            blocked = [p for p in positions.values() if p.get("settlement_blocked")]
            if blocked:
                with st.container(border=True):
                    st.warning(
                        f"{len(blocked)} position(s) are past their resolution deadline but their "
                        f"outcome could not be confirmed, so they have **not** been settled. "
                        f"Use **Sync** to reconcile against Polymarket, or settle them manually below."
                    )
                    for bp in blocked:
                        st.caption(f"• **{str(bp.get('question',''))[:60]}** — {bp.get('settlement_blocked')}")
            with st.expander("1-Click Verification, Exit & Settlement", icon=":material/search:"):
                for tid, p in list(positions.items()):
                    slug_val = p.get("slug") or database.resolve_market_slug(p.get("market_id"))
                    poly_url = database.get_polymarket_url(slug_val, p.get("market_id"))
                    qc1, qc2, qc3, qc4 = st.columns([2.2, 0.8, 0.8, 0.8])
                    with qc1:
                        acc_lbl = f"[{p.get('account_name', config.DEFAULT_ACCOUNT_NAME)}] " if p.get("account_name") else ""
                        st.markdown(f"**{acc_lbl}{p.get('question', '')}** — `{p.get('outcome_label', '')}` · Entry: **{p.get('entry_price', 0):.2f}** · Capital: **${p.get('stake', 0):.2f}**")
                    with qc2:
                        st.link_button("Market", poly_url, icon=":material/open_in_new:", width="stretch")
                    with qc3:
                        exit_pop = st.popover("Exit", icon=":material/logout:", width="stretch")
                        with exit_pop:
                            st.markdown(f"**Exit: {p.get('outcome_label', '')}**")
                            token_id_str = str(p.get("token_id", tid))
                            shares_held = float(p.get("shares") or (p.get("stake", 1.0) / max(0.01, p.get("entry_price", 0.95))))
                            entry_p = float(p.get("entry_price", 0.95))
                            invested = float(p.get("stake", 1.0))
                            pos_mode = str(p.get("mode", execution_mode_str)).upper()
                            pos_acc = p.get("account_name", config.DEFAULT_ACCOUNT_NAME)

                            live_bid = get_live_token_best_bid(token_id_str)
                            default_exit = live_bid if (live_bid and live_bid > 0.0) else entry_p
                            safe_default_exit = float(max(0.0001, min(1.0000, float(default_exit or 0.5))))

                            st.caption(f"Tokens: `{shares_held:.2f}` | Entry: `${entry_p:.4f}`")
                            if live_bid:
                                st.info(f"Live Best Bid on CLOB: **${live_bid:.4f}**")
                            else:
                                st.caption("No immediate bids in book; using entry price.")

                            exit_p = st.number_input(
                                "Exit Price ($)",
                                min_value=0.0001,
                                max_value=1.0000,
                                value=safe_default_exit,
                                step=0.001,
                                format="%.4f",
                                key=f"ov_exit_p_{tid}",
                            )
                            est_return = shares_held * exit_p
                            est_pnl = est_return - invested
                            pnl_pct = (est_pnl / invested * 100.0) if invested > 0 else 0.0
                            pnl_color = "green" if est_pnl >= 0 else "red"
                            st.markdown(f"Est. Return: **${est_return:.2f}** | P&L: :{pnl_color}[**${est_pnl:+.2f} ({pnl_pct:+.1f}%)**]")

                            exit_order_type = st.selectbox(
                                "Exit Order Type",
                                options=["LIMIT", "MARKET"],
                                index=0,
                                key=f"ov_exit_type_{tid}"
                            )

                            exit_btn_label = "Sell on CLOB" if pos_mode == "LIVE" else "Close Paper"
                            if st.button(exit_btn_label, icon=":material/point_of_sale:", type="primary", key=f"ov_btn_exit_{tid}", width="stretch"):
                                if execute_exit(tid, p, exit_p, order_type=exit_order_type):
                                    st.rerun()
                    with qc4:
                        pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                        with pop:
                            st.caption(f"Settle {p.get('question')[:30]}...")
                            if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"ov_won_{tid}", width="stretch"):
                                broker.force_settle_position(tid, won=True, note="Manual settlement via Dashboard: WON")
                                if p.get("trade_id"):
                                    database.force_settle_orphaned_trade(p.get("trade_id"), won=True, note="Manual settlement via Dashboard: WON")
                                _cached_all_trades.clear()
                                st.success("Settled as WON!")
                                st.rerun()
                            if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"ov_lost_{tid}", width="stretch"):
                                broker.force_settle_position(tid, won=False, note="Manual settlement via Dashboard: LOST")
                                if p.get("trade_id"):
                                    database.force_settle_orphaned_trade(p.get("trade_id"), won=False, note="Manual settlement via Dashboard: LOST")
                                _cached_all_trades.clear()
                                st.warning("Settled as LOST.")
                                st.rerun()

    with col_gates:
        st.markdown("##### Circuit Breakers")
        st.dataframe(pd.DataFrame(build_gates_data())[["Rule", "Value"]], hide_index=True, width="stretch")

    st.write("")
    st.markdown("##### Live Market Signals")
    st.markdown('<div class="sst-section-sub">Opportunities passing your price, liquidity, and resolution-window filters right now.</div>', unsafe_allow_html=True)
    @st.fragment(run_every="10s")
    def render_signals_table():
        broker = get_broker()
        state = broker.reload()
        current_signals = state.get("signals", [])
        if not current_signals:
            st.info("No signals matching current threshold criteria in the latest scan. Click 'SCAN NOW' above or wait for the next loop.")
        else:
            df_signals = []
            for s in current_signals:
                slug = s.get("slug", "") or database.resolve_market_slug(s.get("market_id"))
                poly_url = database.get_polymarket_url(slug, s.get("market_id"))
                conf_price = float(s.get('confirmed_price', 0) or 0.0)
                df_signals.append({
                    "Polymarket": poly_url,
                    "Match / Game": s.get("question"),
                    "Outcome": s.get("outcome_label"),
                    "Price": conf_price,
                    "Implied Win %": round(conf_price * 100, 1),
                    "24h Volume": float(s.get('volume', 0) or 0.0),
                    "Liquidity": float(s.get('liquidity', 0) or 0.0),
                    "End Date": s.get("end_date", "N/A"),
                })
            st.dataframe(
                pd.DataFrame(df_signals),
                column_config={
                    "Polymarket": st.column_config.LinkColumn("Polymarket", display_text="View Market ↗"),
                    "Price": st.column_config.NumberColumn("Price", format="$%.3f"),
                    "Implied Win %": st.column_config.ProgressColumn("Implied Win %", format="%.1f%%", min_value=0, max_value=100),
                    "24h Volume": st.column_config.NumberColumn("24h Volume", format="$%d"),
                    "Liquidity": st.column_config.NumberColumn("Liquidity", format="$%d"),
                },
                hide_index=True,
                width="stretch",
            )
            
    render_signals_table()

    if signals:

        with st.expander("Manual Trade Trigger", icon=":material/rocket_launch:", expanded=False):
            st.write(f"Execute a trade in **{execution_mode_str}** mode on one of the detected signals:")
            signal_options = {f"{s['question'][:60]} ({s['outcome_label']} @ {s['confirmed_price']:.3f})": s for s in signals}
            selected_signal_name = st.selectbox("Select Signal to Trade", list(signal_options.keys()), key="overview_signal_select")

            selected_sig = signal_options[selected_signal_name]
            sel_slug = selected_sig.get("slug") or database.resolve_market_slug(selected_sig.get("market_id"))
            sel_url = database.get_polymarket_url(sel_slug, selected_sig.get("market_id"))
            st.link_button(f"Inspect '{selected_sig.get('question')[:45]}...' on Polymarket", sel_url, icon=":material/open_in_new:")

            col_stake, col_type, col_btn = st.columns([1.5, 1.5, 1.5])
            with col_stake:
                manual_stake = st.number_input(
                    "Trade Stake ($)",
                    min_value=1.0,
                    max_value=500.0,
                    value=float(max(1.0, min(500.0, float(settings.get("stake_per_trade", 25.0) or 25.0)))),
                    step=5.0,
                    key="overview_manual_stake_input",
                )
            with col_type:
                order_type = st.selectbox(
                    "Order Type",
                    options=["LIMIT", "MARKET"],
                    index=0,
                    help="LIMIT waits for your exact price (can be partially filled or rest in the book). MARKET executes immediately at the best available price (bypasses size minimums but risks slippage).",
                    key="overview_manual_order_type",
                )
            with col_btn:
                st.write("")
                st.write("")
                btn_label = f"Open {execution_mode_str} Position"
                if st.button(btn_label, icon=":material/rocket_launch:", width="stretch", key="overview_trade_btn"):
                    target_sig = signal_options[selected_signal_name]

                    class SigObj:
                        pass

                    obj = SigObj()
                    for k, v in target_sig.items():
                        setattr(obj, k, v)

                    if is_live:
                        # The kill switch is meant to block ALL new entries. The manual
                        # trigger used to ignore it entirely, so the PANIC button stopped
                        # the bot but left this button live.
                        if kill_switch_active:
                            st.error("Entry Kill Switch is ACTIVE -- new entries are blocked. Disable it in Control and Risk first.")
                            st.stop()

                        live_inst = live_broker.get_live_broker()
                        if live_inst:
                            # Check each account's own risk budget BEFORE sending money to
                            # the exchange. This used to place first and check second, so a
                            # limit breach left a real order on-chain with no local record.
                            eligible = {}
                            for acc_name in live_inst.get_account_names():
                                acc_settings = settings_manager.get_account_settings(acc_name)
                                if acc_settings.get("bot_status", "RUNNING") == "PAUSED":
                                    st.warning(f"Skipping `{acc_name}`: account is paused.")
                                    continue
                                if acc_settings.get("entry_kill_switch", False):
                                    st.warning(f"Skipping `{acc_name}`: account kill switch is active.")
                                    continue
                                ok_acc, why_acc = broker.can_open(
                                    manual_stake,
                                    account_name=acc_name,
                                    limits={
                                        "max_open_positions": acc_settings.get("max_open_positions"),
                                        "max_total_exposure": acc_settings.get("max_total_exposure"),
                                        "max_trades_per_day": acc_settings.get("max_trades_per_day"),
                                    },
                                )
                                if not ok_acc:
                                    st.warning(f"Skipping `{acc_name}`: {why_acc}")
                                    continue
                                eligible[acc_name] = float(manual_stake)

                            if not eligible:
                                st.error("No account can take this trade right now.")
                                st.stop()

                            try:
                                results = live_inst.place_buy_selected(obj.token_id, obj.confirmed_price, eligible, order_type=order_type)
                                any_change = False
                                for res in results:
                                    if res["success"]:
                                        any_change = True
                                        # force=True: the shares are already ours, so record
                                        # them even if a cap moved underneath us. Untracked
                                        # live exposure is the worse failure.
                                        broker.open_position(
                                            obj,
                                            stake=res["stake"],
                                            mode="LIVE",
                                            account_name=res["account_name"],
                                            wallet_address=res["wallet"],
                                            filled_size=res["filled_size"],
                                            fill_price=res["avg_price"],
                                            order_id=res.get("order_id"),
                                            force=True,
                                        )
                                        st.success(
                                            f"`{res['account_name']}`: filled {res['filled_size']:.2f} shares "
                                            f"@ ${res['avg_price']:.4f} (${res['filled_cost']:.2f})."
                                        )
                                    elif res["resting"]:
                                        broker.record_unfilled()
                                        st.warning(
                                            f"`{res['account_name']}`: order accepted but resting unfilled "
                                            f"(order {res.get('order_id')}). No position was opened -- cancel it "
                                            f"from Control and Risk if you no longer want the entry."
                                        )
                                    else:
                                        st.error(f"`{res['account_name']}`: {res['error']}")
                                if any_change:
                                    _cached_all_trades.clear()
                                    st.rerun()
                                else:
                                    st.stop()
                            except Exception as e:
                                st.error(f"Failed to execute live orders: {e}")
                                st.stop()
                        else:
                            st.error("Live broker not ready. Check credentials.")
                            st.stop()
                    else:
                        pos, reason = broker.open_position(obj, stake=manual_stake, mode="PAPER")
                        if pos:
                            _cached_all_trades.clear()
                            st.success(f"Successfully opened paper position on {obj.question[:40]} with ${manual_stake} stake!")
                            st.rerun()
                        else:
                            st.error(f"Cannot open position: {reason}")


# =============================================================
# SCREEN 2: CONTROL AND RISK
# Manual controls (live trading toggle, kill switch) + full risk-limit visibility.
# =============================================================
with tab_control:
    st.warning("These controls directly affect real capital on Polymarket mainnet.")

    live_enabled_label = "TRUE" if is_live else "FALSE"
    kill_switch_label = "ACTIVE" if kill_switch_active else "OFF"
    creds_label = "CONFIGURED" if creds_ok else "MISSING"

    with st.container(horizontal=True):
        with st.container(border=True):
            toggle_live = st.toggle("Live Trading", value=is_live, help="Enables real on-chain execution via Polymarket CLOB. Requires PRIVATE_KEY in .env.")
            st.caption("Real on-chain execution via CLOB. Requires configured credentials.")
            if toggle_live != is_live:
                if toggle_live:
                    if not creds_ok:
                        st.error(f"Cannot enable Live Trading: {creds_msg}. Configure PRIVATE_KEY in your .env file first.")
                    else:
                        settings_manager.update_setting("live_trading", True)
                        st.success("LIVE Trading ENABLED! The background loop will trade live on next cycle.")
                        st.rerun()
                else:
                    settings_manager.update_setting("live_trading", False)
                    st.info("LIVE Trading DISABLED. Bot switched back to PAPER mode.")
                    st.rerun()

        with st.container(border=True):
            toggle_kill = st.toggle("Entry Kill Switch", value=kill_switch_active, help="Immediately halts opening any new orders while leaving position tracking and resolution active.")
            st.caption("Blocks new entries; existing positions still tracked to resolution.")
            if toggle_kill != kill_switch_active:
                settings_manager.update_setting("entry_kill_switch", toggle_kill)
                st.rerun()

        with st.container(border=True):
            st.metric("Credentials", creds_label, delta="VERIFIED" if creds_ok else "MISSING", delta_color="normal" if creds_ok else "inverse")
            st.caption(f"{len(config.get_configured_accounts())} account(s) connected")

    if not is_live:
        st.info("Current EXECUTION_MODE is **PAPER**. Toggle **'Live Trading'** above to switch dynamically without restarting the bot.")
    else:
        st.warning("Current EXECUTION_MODE is **LIVE**. Real orders are placed on Polymarket mainnet.")

    st.write("")
    col_accounts, col_limits = st.columns([1.3, 1])
    with col_accounts:
        st.markdown("##### Connected Accounts")
        configured_accounts = config.get_configured_accounts()
        if configured_accounts:
            acc_rows = []
            for acc in configured_accounts:
                stake = settings_manager.get_account_stake(acc["name"], fallback=acc.get("stake", config.STAKE_PER_TRADE))
                acc_rows.append({
                    "Account": acc["name"],
                    "Wallet": acc.get("funder_address") or "(derived from key)",
                    "Stake": f"${stake:.2f}",
                    "Status": "🟢 Enabled" if acc.get("enabled", True) else "⚪ Disabled",
                })
            st.dataframe(pd.DataFrame(acc_rows), hide_index=True, width="stretch")
        else:
            st.info("No trading accounts configured yet.")

    with col_limits:
        st.markdown("##### Strategy and Limits")
        limits_data = [
            {"Setting": "Entry Price Band",
             "Value": "{:.3f} - {:.3f}".format(*settings_manager.effective_price_band(settings))},
            {"Setting": "Min 24h Volume", "Value": f"${float(settings.get('min_volume', 5000.0)):,.0f}"},
            {"Setting": "Max Open Positions", "Value": str(int(settings.get("max_open_positions", 10)))},
            {"Setting": "Max Trades / Day", "Value": str(int(settings.get("max_trades_per_day", 10)))},
        ]
        st.dataframe(pd.DataFrame(limits_data), hide_index=True, width="stretch")

    st.write("")
    st.markdown("##### Full Circuit Breaker and Risk Gate Table")
    gates_df = pd.DataFrame(build_gates_data())
    st.dataframe(
        gates_df,
        hide_index=True,
        width="stretch",
        column_config={"Status": st.column_config.TextColumn("Status")},
    )

    st.write("")
    with st.expander("Engine Controls (order limits & slippage)", icon=":material/settings:"):
        cp_col1, cp_col2 = st.columns(2)
        with cp_col1:
            current_max_pos = int(settings.get("max_open_positions", 10))
            max_pos_input = st.number_input("Max Open Positions", min_value=1, max_value=50, value=current_max_pos, step=1)
            if max_pos_input != current_max_pos:
                settings_manager.update_setting("max_open_positions", max_pos_input)
        with cp_col2:
            current_daily_limit = int(settings.get("max_trades_per_day", 10))
            daily_limit_input = st.number_input("Daily Trade Limit", min_value=1, max_value=100, value=current_daily_limit, step=1)
            if daily_limit_input != current_daily_limit:
                settings_manager.update_setting("max_trades_per_day", daily_limit_input)

            current_slippage = float(settings.get("max_slippage", 0.005))
            slippage_input = st.number_input(
                "Max Slippage Price", min_value=0.001, max_value=0.05, value=current_slippage, step=0.001, format="%.3f",
                help="Maximum allowed difference between quoted price and fill price.",
            )
            if slippage_input != current_slippage:
                settings_manager.update_setting("max_slippage", slippage_input)

    with st.expander("Add / Manage Trading Accounts", icon=":material/add:", expanded=not creds_ok):
        st.caption("Add additional Polymarket wallets to trade from. Credentials are written to your local `.env` file only and are never sent anywhere else.")

        configured_accounts = config.get_configured_accounts()
        if configured_accounts:
            st.markdown("**Configured Accounts**")
            for acc in configured_accounts:
                mc1, mc2, mc3, mc4, mc5, mc6 = st.columns([1.8, 2.2, 1.1, 1, 1, 1.2])
                with mc1:
                    st.text(acc["name"])
                with mc2:
                    st.text(acc.get("funder_address") or "(derived from key)")
                with mc3:
                    st.text("🟢 Enabled" if acc.get("enabled", True) else "⚪ Disabled")
                with mc4:
                    toggle_label = "Disable" if acc.get("enabled", True) else "Enable"
                    if st.button(toggle_label, key=f"toggle_acc_{acc['id']}"):
                        config.set_account_enabled(acc["id"], not acc.get("enabled", True))
                        live_broker.invalidate_live_broker_cache()
                        st.rerun()
                with mc5:
                    confirm_key = f"confirm_remove_{acc['id']}"
                    if st.session_state.get(confirm_key):
                        if st.button("Confirm?", key=f"confirm_btn_{acc['id']}", type="primary"):
                            config.remove_account(acc["id"])
                            live_broker.invalidate_live_broker_cache()
                            st.session_state.pop(confirm_key, None)
                            st.success(f"Removed {acc['name']}.")
                            st.rerun()
                    else:
                        if st.button("Remove", key=f"remove_acc_{acc['id']}"):
                            st.session_state[confirm_key] = True
                            st.rerun()
                with mc6:
                    has_relayer = bool(acc.get("relayer_api_key"))
                    relayer_pop = st.popover(
                        "⛽ Gasless" if has_relayer else "⛽ Set Relayer",
                        width="stretch",
                        help="Configure a Polymarket Relayer API key so this account's orders submit gaslessly instead of paying MATIC gas.",
                    )
                    with relayer_pop:
                        st.caption(
                            f"**{acc['name']}** currently "
                            + ("uses a Relayer API key (gasless submission)." if has_relayer
                               else "has no Relayer API key set -- it pays its own MATIC gas per order.")
                        )
                        st.caption("Create one at polymarket.com → Settings → Relayer API keys, then paste both values below.")
                        r_key = st.text_input("Relayer API Key", type="password", placeholder="Paste RELAYER_API_KEY", key=f"relayer_key_{acc['id']}")
                        r_addr = st.text_input("Relayer API Key Address", placeholder="0x... (Signer Address from the same page)", key=f"relayer_addr_{acc['id']}")
                        rc1, rc2 = st.columns(2)
                        with rc1:
                            if st.button("Save", key=f"relayer_save_{acc['id']}", type="primary", width="stretch"):
                                if r_key.strip() and r_addr.strip():
                                    config.set_account_relayer(acc["id"], r_key, r_addr)
                                    live_broker.invalidate_live_broker_cache()
                                    st.success(f"Relayer credentials saved for {acc['name']}.")
                                    st.rerun()
                                else:
                                    st.warning("Both the Relayer API Key and its Address are required.")
                        with rc2:
                            if has_relayer and st.button("Clear", key=f"relayer_clear_{acc['id']}", width="stretch"):
                                config.set_account_relayer(acc["id"], "", "")
                                live_broker.invalidate_live_broker_cache()
                                st.info(f"Relayer credentials cleared for {acc['name']} (will pay its own gas, or fall back to the global RELAYER_API_KEY if set).")
                                st.rerun()
        else:
            st.info("No trading accounts configured yet. Add one below to enable live trading.")

        st.markdown("**Add New Account**")
        with st.form("add_account_form", clear_on_submit=True):
            new_name = st.text_input("Account Name", placeholder="e.g. Secondary_Safe")
            new_pk = st.text_input("Private Key", type="password", placeholder="0x...")
            new_funder = st.text_input("Funder / Proxy Wallet Address (optional)", placeholder="0x...")
            new_stake = st.number_input(
                "Stake Per Trade ($, optional override)",
                min_value=0.0, value=0.0, step=5.0,
                help="Leave at 0 to use the global stake-per-trade setting for this account.",
            )
            st.caption("Optional -- for gasless order submission via Polymarket's Relayer (Settings → Relayer API keys on polymarket.com). Leave blank to pay MATIC gas directly, or to inherit the global RELAYER_API_KEY from `.env`.")
            new_relayer_key = st.text_input("Relayer API Key (optional)", type="password", placeholder="Paste RELAYER_API_KEY")
            new_relayer_addr = st.text_input("Relayer API Key Address (optional)", placeholder="0x... (Signer Address from the same page)")
            submitted = st.form_submit_button("Add Account", type="primary")
            if submitted:
                try:
                    idx = config.add_account(
                        name=new_name,
                        private_key=new_pk,
                        funder_address=new_funder,
                        stake=new_stake if new_stake > 0 else None,
                        relayer_api_key=new_relayer_key,
                        relayer_api_key_address=new_relayer_addr,
                    )
                    live_broker.invalidate_live_broker_cache()
                    st.success(f"Account added (slot {idx}). Refreshing...")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    if not creds_ok:
        st.info("""
Live data will populate here when LIVE mode is running and readiness is READY.
Add a trading account above, or add credentials directly to your `.env` file:
```bash
# Single-Account format:
PRIVATE_KEY=0x_your_wallet_private_key
FUNDER_ADDRESS=0x_your_polymarket_profile_wallet_address  # Optional

# Multi-Account format:
ACCOUNT_1_NAME=MetaMask_Main
ACCOUNT_1_PRIVATE_KEY=0x_first_private_key
ACCOUNT_1_FUNDER_ADDRESS=
ACCOUNT_1_STAKE=25.0
```
""")
    else:
        live_instance = live_broker.get_live_broker()
        if live_instance:
            acc_names = live_instance.get_account_names()
            acc_filter_choices = ["Combined (All Accounts)"] + acc_names
            sel_acc = st.segmented_control("Select Account View", acc_filter_choices, default="Combined (All Accounts)", key="control_acc_select") or "Combined (All Accounts)"

            if sel_acc == "Combined (All Accounts)":
                agg = _cached_live_aggregated_vitals()
                all_orders = _cached_live_open_orders()
                all_pos = _cached_live_positions()

                with st.container(horizontal=True):
                    st.metric("Active Accounts", agg['account_count'], border=True)
                    st.metric("Pooled Collateral (USDC.e)", f"${agg['total_collateral']:,.2f}", border=True)
                    st.metric("Total Open Orders", len(all_orders), border=True)
                    st.metric("Total Live Positions", len(all_pos), border=True)

                if len(agg["accounts"]) > 1:
                    st.markdown("##### ⚡ Dynamic Per-Account Stake Sizing")
                    stk_cols = st.columns(len(agg["accounts"]))
                    for idx, a in enumerate(agg["accounts"]):
                        acc_n = a["name"]
                        cur_acc_stk = settings_manager.get_account_stake(acc_n, fallback=a.get("stake", config.STAKE_PER_TRADE))
                        with stk_cols[idx]:
                            new_val = st.number_input(
                                f"{acc_n} Stake ($)", min_value=1.0, max_value=1000.0, value=float(cur_acc_stk), step=5.0,
                                key=f"control_acc_stk_{acc_n}",
                            )
                            if new_val != cur_acc_stk:
                                settings_manager.set_account_stake(acc_n, new_val)
                                st.success(f"Updated {acc_n} stake to ${new_val:.2f}!")
                                st.rerun()

                if len(agg["accounts"]) >= 1:
                    st.markdown("##### 🎛️ Per-Account Independent Controls")
                    pac_acc = st.selectbox("Configure Account", acc_names, key="pac_select")
                    pac_settings = settings_manager.get_account_settings(pac_acc)
                    pac_overrides = settings_manager.load_settings().get("account_overrides", {}).get(pac_acc, {})

                    pac_col1, pac_col2 = st.columns(2)
                    with pac_col1:
                        pac_status = st.selectbox(
                            "Status", ["RUNNING", "PAUSED"],
                            index=0 if pac_settings.get("bot_status", "RUNNING") == "RUNNING" else 1,
                            key=f"pac_status_{pac_acc}",
                        )
                        pac_kill = st.toggle("Kill Switch", value=bool(pac_settings.get("entry_kill_switch", False)), key=f"pac_kill_{pac_acc}")
                        pac_max_pos = st.number_input("Max Open Positions", min_value=1, max_value=200, value=int(pac_settings.get("max_open_positions") or 10), key=f"pac_maxpos_{pac_acc}")
                        pac_max_exp = st.number_input("Max Total Exposure ($)", min_value=1.0, value=float(pac_settings.get("max_total_exposure") or 200.0), key=f"pac_maxexp_{pac_acc}")
                        pac_max_trades = st.number_input("Max Trades / Day", min_value=1, max_value=500, value=int(pac_settings.get("max_trades_per_day") or 10), key=f"pac_maxtrades_{pac_acc}")
                    with pac_col2:
                        # get_account_settings already resolves an un-overridden band
                        # to whatever is in force, so this seeds to the global band and
                        # only narrows it where the account has chosen to.
                        pac_band_low, pac_band_high = settings_manager.effective_price_band(settings)
                        pac_price_min, pac_price_max = st.slider(
                            "Price Band", min_value=0.5, max_value=1.0,
                            value=(float(pac_settings.get("price_min") or pac_band_low),
                                   float(pac_settings.get("price_max") or pac_band_high)),
                            step=0.001, format="%.3f", key=f"pac_price_{pac_acc}",
                            help=f"Narrows this account within the global band "
                                 f"({pac_band_low:.3f}-{pac_band_high:.3f}). A value outside "
                                 f"that range has no effect - the scan never offers "
                                 f"prices beyond it.",
                        )
                        pac_min_vol = st.number_input("Min Volume ($)", min_value=0.0, value=float(pac_settings.get("min_volume") if pac_settings.get("min_volume") is not None else 5000.0), key=f"pac_minvol_{pac_acc}")
                        pac_min_liq = st.number_input("Min Liquidity ($)", min_value=0.0, value=float(pac_settings.get("min_liquidity") if pac_settings.get("min_liquidity") is not None else 1000.0), key=f"pac_minliq_{pac_acc}")
                        pac_sports_types = st.multiselect("Sports Market Types", ["moneyline", "spread", "totals"], default=pac_settings.get("sports_market_types") or ["moneyline"], key=f"pac_sports_{pac_acc}")

                    pac_apply_col, pac_reset_col = st.columns([1, 1])
                    with pac_apply_col:
                        if st.button(f"Apply Independent Settings for {pac_acc}", key=f"pac_apply_{pac_acc}", type="primary"):
                            settings_manager.set_account_override(pac_acc, "bot_status", pac_status)
                            settings_manager.set_account_override(pac_acc, "entry_kill_switch", pac_kill)
                            settings_manager.set_account_override(pac_acc, "max_open_positions", int(pac_max_pos))
                            settings_manager.set_account_override(pac_acc, "max_total_exposure", float(pac_max_exp))
                            settings_manager.set_account_override(pac_acc, "max_trades_per_day", int(pac_max_trades))
                            settings_manager.set_account_override(pac_acc, "price_min", float(pac_price_min))
                            settings_manager.set_account_override(pac_acc, "price_max", float(pac_price_max))
                            settings_manager.set_account_override(pac_acc, "min_volume", float(pac_min_vol))
                            settings_manager.set_account_override(pac_acc, "min_liquidity", float(pac_min_liq))
                            settings_manager.set_account_override(pac_acc, "sports_market_types", pac_sports_types)
                            st.success(f"Independent settings applied for {pac_acc}.")
                            st.rerun()
                    with pac_reset_col:
                        if pac_overrides and st.button(f"Reset {pac_acc} to Global Defaults", key=f"pac_reset_{pac_acc}"):
                            for key in list(pac_overrides.keys()):
                                settings_manager.clear_account_override(pac_acc, key)
                            st.success(f"{pac_acc} reset to global defaults.")
                            st.rerun()

                    if pac_overrides:
                        st.caption(f"⚡ {pac_acc} currently overrides: {', '.join(sorted(pac_overrides.keys()))}")

                st.markdown("##### Open CLOB Orders (All Accounts)")
                if not all_orders:
                    st.info("No open CLOB limit orders across any account.")
                else:
                    render_open_orders(all_orders, account_name=None, key_prefix="ctrl_all")

                st.markdown("##### On-Chain Positions (All Accounts)")
                if not all_pos:
                    st.info("No open on-chain positions returned across accounts.")
                else:
                    st.dataframe(pd.DataFrame(all_pos), hide_index=True)

            else:
                session = live_instance.get_session(sel_acc)
                if session:
                    vitals = _cached_account_vitals(sel_acc)
                    with st.container(horizontal=True):
                        st.metric("Connected Wallet", f"{vitals['wallet'][:6]}...{vitals['wallet'][-4:]}", help=vitals['wallet'], border=True)
                        st.metric("Wallet Type", vitals['wallet_type'], border=True)
                        st.metric("Collateral (USDC.e)", f"${vitals['collateral_balance']:,.2f}", border=True)
                        st.metric("Stake Per Trade", f"${vitals['stake']:.2f}", border=True)

                    cur_single_stk = settings_manager.get_account_stake(vitals["name"], fallback=vitals.get("stake", config.STAKE_PER_TRADE))
                    new_single_stk = st.number_input(
                        f"Update {vitals['name']} Stake ($)", min_value=1.0, max_value=1000.0, value=float(max(1.0, min(1000.0, float(cur_single_stk or 1.0)))), step=5.0,
                        key=f"single_acc_stk_{vitals['name']}",
                    )
                    if new_single_stk != cur_single_stk:
                        settings_manager.set_account_stake(vitals["name"], new_single_stk)
                        st.success(f"Updated {vitals['name']} stake to ${new_single_stk:.2f}!")
                        st.rerun()

                    st.markdown(f"##### Open CLOB Orders — `{vitals['name']}`")
                    live_orders = _cached_live_open_orders(sel_acc)
                    if not live_orders:
                        st.info(f"No open CLOB limit orders for {vitals['name']}.")
                    else:
                        render_open_orders(live_orders, account_name=sel_acc, key_prefix=f"ctrl_{sel_acc}")

                    st.markdown(f"##### On-Chain Positions — `{vitals['name']}`")
                    live_pos = _cached_live_positions(sel_acc)
                    if not live_pos:
                        st.info(f"No open on-chain positions for {vitals['name']}.")
                    else:
                        st.dataframe(pd.DataFrame(live_pos), hide_index=True)
        else:
            st.warning("Could not initialize Live Broker instance. Check terminal logs for details.")

    with st.expander("Lifecycle State & Activity Logs", icon=":material/medical_services:"):
        with st.container(horizontal=True):
            st.metric("Dashboard State", status, delta="ACTIVE" if status == "RUNNING" else "HALTED", delta_color="normal" if status == "RUNNING" else "inverse", border=True)
            st.metric("Execution Mode", execution_mode_str, delta="MAINNET" if is_live else "SANDBOX", delta_color="normal" if is_live else "off", border=True)
            st.metric("System State Store", "CONNECTED", delta="SQLite & State JSON", border=True)
        logs = state.get("logs", [])
        if not logs:
            st.info("No activity logs recorded yet.")
        else:
            df_logs = [{"Time": item.get("timestamp", "")[:19].replace("T", " "), "Level": item.get("level", "INFO"), "Message": item.get("message", "")} for item in reversed(logs[-100:])]
            st.dataframe(pd.DataFrame(df_logs), hide_index=True)


# =============================================================
# SCREEN 3: HISTORY AND PERFORMANCE
# Trade history and realized performance (paper + live + on-chain).
# =============================================================
with tab_history:
    perf_options = ["Live Execution Portfolio (On-Chain)", "Paper Simulation Portfolio"]
    default_perf = perf_options[0] if is_live else perf_options[1]
    perf_portfolio_view = st.segmented_control("Select Portfolio View", perf_options, default=default_perf, key="hist_perf_view") or default_perf

    # Initialised up front: when the Live portfolio view is selected but the broker
    # can't be built (missing/invalid credentials), the branch below only emitted a
    # warning and never assigned these -- so the rest of the screen crashed with
    # NameError: active_trades_for_table.
    active_trades_for_table = []
    active_pos_for_expander = None
    active_orders_for_expander = None

    if perf_portfolio_view == "Live Execution Portfolio (On-Chain)":
        live_inst = live_broker.get_live_broker()
        if not live_inst:
            st.warning("Live trading credentials are not configured or invalid in `.env`. Check credentials in the **Control and Risk** screen.")
            # Fall back to the locally recorded live trades so history stays readable.
            active_trades_for_table = _cached_all_trades(broker_filter="live")
        else:
            live_acc_choices = ["All Accounts (Combined)"] + live_inst.get_account_names()
            selected_perf_acc = st.segmented_control("Portfolio Account Filter", live_acc_choices, default="All Accounts (Combined)", key="hist_perf_acc") or "All Accounts (Combined)"
            is_combined = selected_perf_acc == "All Accounts (Combined)"
            target_acc = None if is_combined else selected_perf_acc

            with st.spinner("Fetching live on-chain account metrics..."):
                live_bal = _cached_live_collateral_balance(target_acc)
                live_pos = _cached_live_positions(target_acc)
                live_orders = _cached_live_open_orders(target_acc)

            live_exposure = sum(float(p.get("current_value", 0.0)) for p in live_pos)
            live_trades = _cached_all_trades(broker_filter="live", account_filter=target_acc)
            closed_live = [t for t in live_trades if is_settled_trade(t)]
            wins_live = len([t for t in closed_live if database.classify_result(float(t.get("pnl", 0.0) or 0)) == "WON"])
            losses_live = len([t for t in closed_live if database.classify_result(float(t.get("pnl", 0.0) or 0)) == "LOST"])
            realized_live = sum(float(t.get("pnl", 0.0)) for t in closed_live)
            win_rate_live = (wins_live / len(closed_live) * 100) if closed_live else 0.0
            avg_pnl_live = (realized_live / len(closed_live)) if closed_live else 0.0

            with st.container(horizontal=True):
                st.metric("Total Trades", len(live_trades), border=True)
                st.metric("Win Rate", f"{win_rate_live:.1f}%", f"{wins_live}W / {losses_live}L, {len(closed_live)} settled", border=True)
                st.metric("Realized PnL", f"${realized_live:+,.2f}", border=True)
                st.metric("Avg PnL / Trade", f"${avg_pnl_live:+,.2f}", border=True)

            st.write("")
            st.markdown("##### Realized P&L, Last 10 Trades")
            with st.container(border=True):
                render_pnl_bar_chart(live_trades)

            active_trades_for_table = live_trades
            active_pos_for_expander = live_pos
            active_orders_for_expander = live_orders
    else:
        # Scoped to broker='paper' -- previously this fetched every trade unfiltered,
        # so the "Paper Simulation Portfolio" KPIs silently included LIVE trades too.
        trades_list = _cached_all_trades(broker_filter="paper")
        closed = [t for t in trades_list if is_settled_trade(t)]
        wins = len([t for t in closed if database.classify_result(float(t.get("pnl", 0.0) or 0)) == "WON"])
        realized = sum(float(t.get("pnl", 0.0)) for t in closed)
        win_rate = (wins / len(closed) * 100) if closed else 0.0
        avg_pnl = (realized / len(closed)) if closed else 0.0

        with st.container(horizontal=True):
            st.metric("Total Trades", len(trades_list), border=True)
            st.metric("Win Rate", f"{win_rate:.1f}%", f"{len(closed)} settled", border=True)
            st.metric("Realized PnL", f"${realized:+,.2f}", border=True)
            st.metric("Avg PnL / Trade", f"${avg_pnl:+,.2f}", border=True)

        st.write("")
        st.markdown("##### Realized P&L, Last 10 Trades")
        with st.container(border=True):
            render_pnl_bar_chart(trades_list)

        active_trades_for_table = trades_list
        active_pos_for_expander = None
        active_orders_for_expander = None

    st.write("")
    col_hdr, col_actions = st.columns([2.5, 1.5])
    with col_hdr:
        st.markdown("##### Trade History")
    with col_actions:
        btn_c1, btn_c2 = st.columns(2)
        with btn_c1:
            if st.button("Refresh", icon=":material/refresh:", width="stretch", key="hist_refresh"):
                live_inst = live_broker.get_live_broker()
                if live_inst:
                    with st.spinner("Checking on-chain Polymarket status..."):
                        live_inst.reconcile_positions()
                _cached_all_trades.clear()
                st.rerun()
        with btn_c2:
            if st.button("Settle Completed", icon=":material/done_all:", width="stretch", key="hist_settle"):
                with st.spinner("Checking market resolutions and completed matches..."):
                    settled = broker.check_resolutions()
                    if settled:
                        st.success(f"Settled {len(settled)} completed trades in Local DB & credited balance!")
                    else:
                        st.info("No open trades are ready to settle automatically.")
                _cached_all_trades.clear()
                st.rerun()

    history_filter = st.segmented_control("Filter", ["ALL", "WON", "LOST"], default="ALL", key="hist_result_filter") or "ALL"

    # Uses the same account/broker-scoped list that fed the KPI cards above -- previously
    # this table and the "Active Open Trades" expander below it read the entire, unfiltered
    # trades table regardless of the selected portfolio view or account filter, so they
    # could show trades under a different account/mode than the KPIs directly above them.
    if not active_trades_for_table:
        st.info("No trades recorded in Local DB yet. The bot will record here as soon as orders are entered.")
    else:
        open_trades = [t for t in active_trades_for_table if "PENDING" in str(t.get("result", "")).upper()]
        if open_trades:
            with st.expander(f"Active Open Trades ({len(open_trades)} active)", icon=":material/bolt:", expanded=False):
                st.caption("Inspect live odds on Polymarket or immediately settle completed matches:")
                for ot_idx, ot in enumerate(open_trades):
                    ot_slug = ot.get("slug") or database.resolve_market_slug(ot.get("market_id"))
                    ot_url = database.get_polymarket_url(ot_slug, ot.get("market_id"))
                    ot_tok = ot.get("token_id")
                    ot_key = f"{ot_idx}_{ot.get('trade_id') or ot_tok}"
                    acc_tag = f"[{ot.get('account_name', config.DEFAULT_ACCOUNT_NAME)}] " if ot.get("account_name") else ""
                    o_c1, o_c2, o_c3, o_c4 = st.columns([2.2, 0.8, 0.8, 0.8])
                    with o_c1:
                        st.markdown(f"**{acc_tag}{ot.get('question')}** · `{ot.get('outcome')}` · Entry: **{float(ot.get('entry_price', 0)):.2f}** · Cost: **${float(ot.get('cost', 0)):.2f}**")
                    with o_c2:
                        st.link_button("Market", ot_url, icon=":material/open_in_new:", width="stretch")
                    with o_c3:
                        h_exit_pop = st.popover("Exit", icon=":material/logout:", width="stretch")
                        with h_exit_pop:
                            st.markdown(f"**Exit: {ot.get('outcome', '')}**")
                            ot_tok_str = str(ot_tok)
                            ot_tokens = float(ot.get("tokens") or 0.0)
                            ot_cost = float(ot.get("cost") or 0.0)
                            ot_entry = float(ot.get("entry_price") or 0.0)
                            ot_acc = ot.get("account_name", config.DEFAULT_ACCOUNT_NAME)
                            ot_broker = str(ot.get("broker", "paper")).lower()

                            live_bid = get_live_token_best_bid(ot_tok_str)
                            default_exit = live_bid if (live_bid and live_bid > 0.0) else (ot_entry or 0.95)
                            safe_default_exit = float(max(0.0001, min(1.0000, float(default_exit or 0.5))))
                            st.caption(f"Tokens: `{ot_tokens:.2f}` | Entry: `${ot_entry:.4f}`")
                            if live_bid:
                                st.info(f"Live Best Bid on CLOB: **${live_bid:.4f}**")
                            else:
                                st.caption("No immediate bids in book; using entry price.")

                            exit_p = st.number_input(
                                "Exit Price ($)",
                                min_value=0.0001,
                                max_value=1.0000,
                                value=safe_default_exit,
                                step=0.001,
                                format="%.4f",
                                key=f"h_exit_p_{ot_key}",
                            )
                            est_return = ot_tokens * exit_p
                            est_pnl = est_return - ot_cost
                            pnl_pct = (est_pnl / ot_cost * 100.0) if ot_cost > 0 else 0.0
                            pnl_color = "green" if est_pnl >= 0 else "red"
                            st.markdown(f"Est. Return: **${est_return:.2f}** | P&L: :{pnl_color}[**${est_pnl:+.2f} ({pnl_pct:+.1f}%)**]")

                            exit_order_type = st.selectbox(
                                "Exit Order Type",
                                options=["LIMIT", "MARKET"],
                                index=0,
                                key=f"h_exit_type_{ot_key}"
                            )

                            exit_btn_label = "Sell on CLOB" if ot_broker == "live" else "Close Paper"
                            if st.button(exit_btn_label, icon=":material/point_of_sale:", type="primary", key=f"h_btn_exit_{ot_key}", width="stretch"):
                                # trades.db rows use different key names than state.json
                                # positions; normalise before handing to the shared handler.
                                hist_position = {
                                    "mode": ot_broker.upper(),
                                    "trade_id": ot.get("trade_id"),
                                    "token_id": ot_tok,
                                    "account_name": ot_acc,
                                    "shares": ot_tokens,
                                    "stake": float(ot.get("cost", 0.0) or 0.0),
                                }
                                if execute_exit(ot_tok, hist_position, exit_p, order_type=exit_order_type):
                                    st.rerun()
                    with o_c4:
                        pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                        with pop:
                            st.caption(f"Settle {ot.get('question')[:30]}...")
                            if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"h_won_{ot_key}", width="stretch"):
                                broker.force_settle_position(ot_tok, won=True, note="Manual settlement via Dashboard: WON")
                                if ot.get("trade_id"):
                                    database.force_settle_orphaned_trade(ot.get("trade_id"), won=True, note="Manual settlement via Dashboard: WON")
                                _cached_all_trades.clear()
                                st.success("Settled as WON!")
                                st.rerun()
                            if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"h_lost_{ot_key}", width="stretch"):
                                broker.force_settle_position(ot_tok, won=False, note="Manual settlement via Dashboard: LOST")
                                if ot.get("trade_id"):
                                    database.force_settle_orphaned_trade(ot.get("trade_id"), won=False, note="Manual settlement via Dashboard: LOST")
                                _cached_all_trades.clear()
                                st.warning("Settled as LOST.")
                                st.rerun()

        table_rows = []
        for t in active_trades_for_table:
            res = str(t.get("result", "PENDING")).upper()
            if "WON" in res:
                res_tag = "✅ WON"
            elif "LOST" in res:
                res_tag = "❌ LOST"
            elif res == "VOID":
                res_tag = "🚫 VOID"
            elif res == "EVEN":
                res_tag = "➖ EVEN"
            else:
                res_tag = "⏳ PENDING"
            if history_filter != "ALL" and history_filter not in res:
                continue

            entry_val = float(t.get("entry_price") or 0.0)
            entry_disp = f"{entry_val * 100:.2f}%" if entry_val < 1.0 else f"${entry_val:.2f}"
            pnl_val = float(t.get("pnl") or 0.0)
            pnl_disp = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"
            slug_val = t.get("slug") or database.resolve_market_slug(t.get("market_id"))
            poly_url = database.get_polymarket_url(slug_val, t.get("market_id"))

            table_rows.append({
                "Polymarket": poly_url,
                "Account": str(t.get("account_name", config.DEFAULT_ACCOUNT_NAME)),
                "Placed At": str(t.get("placed_at", ""))[:19].replace("T", " "),
                "Question": str(t.get("question", "")),
                "Outcome": str(t.get("outcome", "")),
                "Entry $": entry_disp,
                "Cost $": f"${float(t.get('cost') or 0.0):.2f}",
                "Result": res_tag,
                "P&L $": pnl_disp,
                "Broker": str(t.get("broker", "paper")).lower(),
            })
        st.dataframe(
            pd.DataFrame(table_rows),
            column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="View Market ↗")},
            hide_index=True,
        )

    with st.expander("Live On-Chain Wallet Activity (Data API)", icon=":material/link:"):
        tracked_addr = settings.get("tracked_wallet_address", "").strip()
        col_addr_in, col_fetch_btn = st.columns([3, 1])
        with col_addr_in:
            addr_val = st.text_input("Target Polymarket / Proxy Wallet Address", value=tracked_addr, placeholder="0x...", key="hist_wallet_addr")
        with col_fetch_btn:
            st.write("")
            st.write("")
            if st.button("Fetch Live Data", icon=":material/search:", width="stretch", key="hist_wallet_fetch"):
                if addr_val.strip() != tracked_addr:
                    settings_manager.update_setting("tracked_wallet_address", addr_val.strip())
                    st.rerun()

        active_addr = addr_val.strip() or tracked_addr
        if not active_addr or not active_addr.startswith("0x") or len(active_addr) < 40:
            st.info("Enter a valid Polymarket wallet address (`0x...`) above to populate live on-chain trades, positions, and P&L.")
        else:
            with st.spinner(f"Fetching on-chain Data API feed for {active_addr[:8]}..."):
                oc_trades, oc_positions, oc_closed, oc_errors = fetch_on_chain_wallet_data(active_addr)

            for oc_err in oc_errors:
                st.error(oc_err)

            oc_t1, oc_t2, oc_t3 = st.tabs([":material/work: Open Positions", ":material/trending_up: Filled Trades", ":material/receipt_long: Settled & Realized P&L"])
            with oc_t1:
                if not oc_positions:
                    st.info(f"No active positions found for {active_addr[:10]}...")
                else:
                    st.dataframe(pd.DataFrame(oc_positions), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="Verify ↗")}, hide_index=True)
            with oc_t2:
                if not oc_trades:
                    st.info(f"No recent filled trades found for {active_addr[:10]}...")
                else:
                    st.dataframe(pd.DataFrame(oc_trades), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="View ↗")}, hide_index=True)
            with oc_t3:
                if not oc_closed:
                    st.info(f"No historical settled positions found for {active_addr[:10]}...")
                else:
                    st.dataframe(pd.DataFrame(oc_closed), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="View ↗")}, hide_index=True)

    with st.expander("Orphaned Trade Reconciliation", icon=":material/search:"):
        st.caption(
            "PENDING trades recorded in `trades.db` with no matching open position in "
            "`state.json` -- e.g. after a portfolio reset or a manual state.json edit that "
            "dropped a position the trade log still remembers. These rows are invisible to "
            "the Overview tab's Open Positions / Exposure (which read state.json only) but "
            "still show up under Active Open Trades below (which reads trades.db). Review "
            "each one and decide: restore it as a tracked open position, settle it as "
            "WON/LOST, or delete it as stale data."
        )
        all_pending = [t for t in get_db_trades(limit=1000) if str(t.get("result", "")).upper() == "PENDING"]
        open_trade_ids = {p.get("trade_id") for p in state.get("positions", {}).values() if p.get("trade_id")}
        open_token_ids = {str(p.get("token_id")) for p in state.get("positions", {}).values()}
        orphaned_trades = [
            t for t in all_pending
            if t.get("trade_id") not in open_trade_ids and str(t.get("token_id")) not in open_token_ids
        ]

        if not orphaned_trades:
            st.success("No orphaned trades found -- every PENDING row has a matching open position.")
        else:
            st.warning(f"{len(orphaned_trades)} orphaned PENDING trade(s) found in trades.db with no matching position in state.json.")
            for orph_idx, ot in enumerate(orphaned_trades):
                rec_key = ot.get("trade_id") or f"orphan_{orph_idx}"
                with st.container(border=True):
                    acc_lbl_o = ot.get("account_name", config.DEFAULT_ACCOUNT_NAME)
                    st.markdown(
                        f"**[{acc_lbl_o}] · `{ot.get('broker', 'paper')}`** — {ot.get('question', '')} "
                        f"· `{ot.get('outcome', '')}` · Entry: **{float(ot.get('entry_price', 0) or 0):.4f}** · "
                        f"Cost: **${float(ot.get('cost', 0) or 0):.2f}** · "
                        f"Placed: {str(ot.get('placed_at', ''))[:19].replace('T', ' ')}"
                    )
                    r_c1, r_c2, r_c3 = st.columns(3)
                    with r_c1:
                        if st.button("Restore as Open Position", icon=":material/history:", key=f"orph_restore_{rec_key}", width="stretch"):
                            broker.restore_position_from_trade(ot)
                            st.success("Restored to state.json as an open position.")
                            st.rerun()
                    with r_c2:
                        settle_pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                        with settle_pop:
                            if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"orph_won_{rec_key}", width="stretch"):
                                database.force_settle_orphaned_trade(ot.get("trade_id"), won=True)
                                _cached_all_trades.clear()
                                st.success("Settled as WON.")
                                st.rerun()
                            if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"orph_lost_{rec_key}", width="stretch"):
                                database.force_settle_orphaned_trade(ot.get("trade_id"), won=False)
                                _cached_all_trades.clear()
                                st.warning("Settled as LOST.")
                                st.rerun()
                    with r_c3:
                        confirm_del_key = f"orph_confirm_del_{rec_key}"
                        if st.session_state.get(confirm_del_key):
                            if st.button("Confirm Delete?", key=f"orph_confirm_btn_{rec_key}", type="primary", width="stretch"):
                                database.delete_trade(ot.get("trade_id"))
                                st.session_state.pop(confirm_del_key, None)
                                _cached_all_trades.clear()
                                st.success("Deleted.")
                                st.rerun()
                        else:
                            if st.button("Delete Row", icon=":material/delete:", key=f"orph_del_{rec_key}", width="stretch"):
                                st.session_state[confirm_del_key] = True
                                st.rerun()

    with st.expander("Paper Portfolio Maintenance", icon=":material/cleaning_services:"):
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            live_tracked = len([p for p in state.get("positions", {}).values() if str(p.get("mode", "PAPER")).upper() == "LIVE"])
            reset_help = "Resets the paper balance and clears PAPER positions only. LIVE positions stay tracked."
            if live_tracked:
                st.caption(f"⚠️ {live_tracked} LIVE position(s) are tracked and will be **kept** — this only clears paper.")
            if st.button("Reset Paper Portfolio", icon=":material/delete_forever:", help=reset_help, width="stretch"):
                kept = broker.reset_paper_portfolio()
                _cached_all_trades.clear()
                st.success(f"Paper portfolio reset. {kept} live position(s) kept.")
                st.rerun()
        with col_r2:
            if st.button("Reset Settings to Defaults", icon=":material/restart_alt:", width="stretch"):
                settings_manager.save_settings(settings_manager.DEFAULT_SETTINGS)
                st.success("Settings restored to factory defaults!")
                st.rerun()


# =============================================================
# SCREEN 4: COLLABORATOR VIEW (read-only)
# Metrics and history only -- no controls, no settings, no trade buttons.
# =============================================================
with tab_collab:
    st.markdown(
        '<div class="sst-readonly-notice"><div class="dot"></div>Read-only view. Controls and settings are hidden for collaborators.</div>',
        unsafe_allow_html=True,
    )

    collab_trades = _cached_all_trades()
    closed_collab = [t for t in collab_trades if is_settled_trade(t)]
    wins_collab = len([t for t in closed_collab if database.classify_result(float(t.get("pnl", 0.0) or 0)) == "WON"])
    losses_collab = len([t for t in closed_collab if database.classify_result(float(t.get("pnl", 0.0) or 0)) == "LOST"])
    realized_collab = sum(float(t.get("pnl", 0.0)) for t in closed_collab)
    win_rate_collab = (wins_collab / len(closed_collab) * 100) if closed_collab else 0.0

    with st.container(horizontal=True):
        st.metric("Net PnL", f"${realized_collab:+,.2f}", f"{len(closed_collab)} settled trades", border=True)
        st.metric("Win Rate", f"{win_rate_collab:.1f}%", f"{wins_collab}W / {losses_collab}L", border=True)
        st.metric("Open Exposure", f"${summary.get('open_exposure', 0.0):,.2f}", border=True)
        st.metric("Reserved Capital", f"${summary.get('reserved_capital', summary.get('open_exposure', 0.0)):,.2f}", border=True)

    st.write("")
    st.markdown("##### Recent Trade History")
    if not collab_trades:
        st.info("No trades recorded yet.")
    else:
        condensed_rows = []
        for t in collab_trades[:30]:
            res = str(t.get("result", "PENDING")).upper()
            if "WON" in res:
                res_tag = "✅ WON"
            elif "LOST" in res:
                res_tag = "❌ LOST"
            elif res == "VOID":
                res_tag = "🚫 VOID"
            elif res == "EVEN":
                res_tag = "➖ EVEN"
            else:
                res_tag = "⏳ PENDING"
            pnl_val = float(t.get("pnl") or 0.0)
            pnl_disp = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"
            condensed_rows.append({
                "Question": str(t.get("question", "")),
                "P&L $": pnl_disp,
                "Result": res_tag,
            })
        st.dataframe(pd.DataFrame(condensed_rows), hide_index=True, width="stretch")

    st.write("")
    st.markdown("##### Realized P&L, Last 10 Trades")
    with st.container(border=True):
        render_pnl_bar_chart(collab_trades)
