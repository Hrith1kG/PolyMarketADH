"""Streamlit Primary Control Panel for Polymarket Sureshot Trading Bot.
Supports seamless switching between Paper and Live Execution, Live Account Vitals,
and full strategy lifecycle monitoring."""
import json
import os
import time
from datetime import datetime, timezone
import altair as alt
import pandas as pd
import streamlit as st

from dotenv import load_dotenv
load_dotenv(override=True)

import config
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
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    :root {
        --bg-page: #0A0D12;
        --bg-header: #0D1118;
        --bg-card-1: #0F1420;
        --bg-card-2: #111622;
        --bg-card-3: #111826;
        --border-card: #1E2733;
        --border-header: #1B2330;
        --divider-row: #161D29;
        --border-dashed: #2A3646;
        --text-primary: #E7ECF3;
        --text-secondary: #C4CDDB;
        --text-muted-1: #9BA8BC;
        --text-muted-2: #8B98AC;
        --text-muted-3: #7C8AA0;
        --accent: #4C8DE8;
        --accent-link: #6BA8F0;
        --success: #4ADE80;
        --success-bg: rgba(74, 222, 128, 0.15);
        --danger: #F87171;
        --danger-bg: rgba(248, 113, 113, 0.15);
    }

    html, body, [class*="css"] {
        font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    .stApp { background: var(--bg-page); }

    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--bg-page); }
    ::-webkit-scrollbar-thumb { background: #26303E; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--accent); }

    /* ---------- Sidebar ---------- */
    section[data-testid="stSidebar"] {
        background: var(--bg-header);
        border-right: 1px solid var(--border-header);
    }
    section[data-testid="stSidebar"] .block-container { padding-top: 1.4rem; }

    /* ---------- Typography ---------- */
    h1, h2, h3 { font-family: 'Space Grotesk', sans-serif; letter-spacing: -0.01em; color: var(--text-primary); }
    h1 { font-weight: 700 !important; }
    h2, h3 { font-weight: 600 !important; }
    p, span, label, .stMarkdown { color: var(--text-muted-1); }
    code, .stCodeBlock, .stCode { font-family: 'IBM Plex Mono', monospace !important; }
    a { color: var(--accent-link) !important; }

    /* ---------- Metric cards ---------- */
    div[data-testid="stMetric"] {
        background: var(--bg-card-2);
        border: 1px solid var(--border-card);
        border-radius: 10px;
        padding: 14px 18px;
    }
    div[data-testid="stMetricLabel"] p {
        font-size: 0.68rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: var(--text-muted-3);
    }
    div[data-testid="stMetricValue"] {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 1.5rem;
        font-weight: 700;
        color: var(--text-primary);
    }
    div[data-testid="stMetricDelta"] { font-family: 'IBM Plex Mono', monospace; }

    /* ---------- Containers used as cards (st.container(border=True)) ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--bg-card-1);
        border: 1px solid var(--border-card) !important;
        border-radius: 12px !important;
    }

    /* ---------- Nav tabs ---------- */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: transparent;
        border-bottom: 1px solid var(--border-header);
    }
    .stTabs [data-baseweb="tab"] {
        height: 40px;
        border-radius: 8px 8px 0 0;
        color: var(--text-muted-3);
        font-family: 'Space Grotesk', sans-serif;
        font-weight: 600;
        font-size: 0.82rem;
        letter-spacing: 0.2px;
        background: transparent;
        border-bottom: 2px solid transparent;
    }
    .stTabs [aria-selected="true"] {
        background: var(--bg-card-3) !important;
        color: var(--text-primary) !important;
        border-bottom: 2px solid var(--accent) !important;
        box-shadow: none !important;
    }

    /* ---------- Buttons ---------- */
    .stButton > button, .stFormSubmitButton > button, .stLinkButton > a {
        border-radius: 8px;
        border: 1px solid var(--border-card);
        background: var(--bg-card-2);
        color: var(--text-primary);
        font-family: 'IBM Plex Mono', monospace;
        font-weight: 600;
        font-size: 0.82rem;
        transition: all 0.15s ease;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stLinkButton > a:hover {
        border-color: var(--accent);
        color: var(--accent-link);
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
        background: var(--accent);
        border: none;
        color: #08121F;
    }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
        filter: brightness(1.08);
        color: #08121F;
    }

    /* ---------- Toggles (Live Trading / Kill Switch pill switches) ---------- */
    div[data-testid="stToggle"] label div[data-baseweb="checkbox"] > div:first-child {
        background: var(--border-card) !important;
    }
    div[data-testid="stToggle"] label div[aria-checked="true"] > div:first-child {
        background: var(--accent) !important;
    }

    /* ---------- Badges / pills ---------- */
    .stBadge, span[data-testid="stBadge"] { font-family: 'IBM Plex Mono', monospace; font-weight: 700 !important; letter-spacing: 0.3px; }

    /* ---------- Inputs, selects, expanders ---------- */
    div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="base-input"] {
        background: var(--bg-header) !important;
        border-color: var(--border-card) !important;
        border-radius: 8px !important;
    }
    .streamlit-expanderHeader, div[data-testid="stExpander"] {
        background: var(--bg-card-2);
        border: 1px solid var(--border-card) !important;
        border-radius: 10px !important;
    }

    /* ---------- Dataframes / tables ---------- */
    div[data-testid="stDataFrame"], div[data-testid="stTable"] {
        border: 1px solid var(--border-card);
        border-radius: 12px;
        overflow: hidden;
    }

    /* ---------- Segmented control (used for filters / view switches) ---------- */
    div[data-testid="stSegmentedControl"] label {
        font-family: 'IBM Plex Mono', monospace !important;
        font-size: 0.75rem !important;
    }

    hr { border-color: var(--border-header) !important; }
    div[data-testid="stAlert"] { border-radius: 10px; border: 1px solid var(--border-card); }

    /* ---------- Header bar ---------- */
    .sst-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
        padding: 16px 24px;
        margin: -1rem -1rem 22px -1rem;
        background: var(--bg-header);
        border-bottom: 1px solid var(--border-header);
    }
    .sst-brand { display: flex; align-items: center; gap: 12px; }
    .sst-logo {
        width: 30px; height: 30px; border-radius: 8px; background: var(--accent);
        display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .sst-logo-mark { width: 10px; height: 10px; background: var(--bg-page); transform: rotate(45deg); }
    .sst-brand-title { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 17px; letter-spacing: 0.3px; color: var(--text-primary); }
    .sst-brand-sub { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-muted-3); letter-spacing: 0.4px; }
    .sst-status-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .sst-status-pill {
        display: flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 20px;
        background: var(--bg-card-3); border: 1px solid var(--border-card);
        font-family: 'IBM Plex Mono', monospace; font-size: 12px;
    }
    .sst-status-pill .dot { width: 7px; height: 7px; border-radius: 50%; }
    .sst-status-pill .lbl { color: var(--text-muted-1); margin-right: 2px; }
    .sst-status-pill .val { font-weight: 600; color: var(--text-primary); }
    .sst-status-pill.kill-active { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); }

    /* ---------- Status banner (Overview) ---------- */
    .sst-banner {
        display: flex; align-items: center; gap: 16px; padding: 18px 22px; border-radius: 12px;
        background: linear-gradient(90deg, #111826, #0F141D); border: 1px solid var(--border-card);
        margin-bottom: 20px; flex-wrap: wrap;
    }
    .sst-banner-icon {
        width: 44px; height: 44px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .sst-banner-icon .core { width: 14px; height: 14px; border-radius: 50%; }
    .sst-banner-title { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 18px; color: var(--text-primary); }
    .sst-banner-sub { font-size: 13px; color: var(--text-muted-2); margin-top: 2px; }

    /* ---------- Read-only notice (Collaborator View) ---------- */
    .sst-readonly-notice {
        display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-radius: 8px;
        background: var(--bg-card-2); border: 1px dashed var(--border-dashed); margin-bottom: 20px;
        font-size: 12px; color: var(--text-muted-2);
    }
    .sst-readonly-notice .dot { width: 8px; height: 8px; border-radius: 50%; background: #5C6B82; flex-shrink: 0; }

    /* ---------- Section subtitle ---------- */
    .sst-section-sub { color: var(--text-muted-3); font-size: 0.85rem; margin-top: -8px; margin-bottom: 18px; }

    /* ---------- ENFORCED / result pills inside markdown ---------- */
    .sst-pill-enforced {
        font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 600; padding: 3px 8px;
        border-radius: 10px; background: var(--success-bg); color: var(--success);
    }
</style>
""", unsafe_allow_html=True)


def get_broker() -> PaperBroker:
    return PaperBroker()


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
            return float(ob.bids[0].price)
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
        err = f"Fetching trades failed: {type(e).__name__}: {e}"
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
        err = f"Fetching open positions failed: {type(e).__name__}: {e}"
        print(f"[dashboard] {err} (address={clean_addr})")
        errors.append(err)

    try:
        closed_paginator = client.list_closed_positions(user=clean_addr)
        for cp in closed_paginator.iter_items():
            avg_p = float(cp.avg_price) if cp.avg_price is not None else 0.0
            cur_p = float(cp.cur_price) if cp.cur_price is not None else 0.0
            pnl_v = float(cp.realized_pnl) if cp.realized_pnl is not None else 0.0
            cp_slug = getattr(cp, "slug", "") or ""
            cp_eslug = getattr(cp, "event_slug", "") or ""
            cp_url = f"https://polymarket.com/market/{cp_slug}" if cp_slug else (f"https://polymarket.com/event/{cp_eslug}" if cp_eslug else "https://polymarket.com")
            closed_pos.append({
                "Polymarket": cp_url,
                "Market / Question": str(cp.title or "")[:50],
                "Outcome": str(cp.outcome or ""),
                "Avg Entry": f"{avg_p * 100:.2f}%" if avg_p < 1.0 else f"${avg_p:.2f}",
                "Exit Price": f"{cur_p * 100:.2f}%" if cur_p < 1.0 else f"${cur_p:.2f}",
                "Cost $": f"${float(cp.total_bought or 0):.2f}",
                "Realized P&L": f"${pnl_v:+.2f}",
                "Closed At": str(cp.timestamp)[:19].replace("T", " ") if cp.timestamp else "",
            })
    except Exception as e:
        err = f"Fetching closed positions failed: {type(e).__name__}: {e}"
        print(f"[dashboard] {err} (address={clean_addr})")
        errors.append(err)

    return trades, positions, closed_pos, errors


def render_pnl_bar_chart(trades_for_chart: list, height: int = 160):
    """Renders the 'Realized P&L, Last 10 Trades' bar chart (green wins / red losses)
    using Altair so per-bar coloring stays a native chart, not raw HTML."""
    closed = [t for t in trades_for_chart if "PENDING" not in str(t.get("result", "")).upper()]
    last10 = closed[-10:]
    if not last10:
        st.info("No settled trades yet to chart.")
        return
    rows = []
    for i, t in enumerate(last10):
        pnl_val = float(t.get("pnl") or 0.0)
        rows.append({
            "idx": i + 1,
            "pnl": pnl_val,
            "outcome": "Win" if pnl_val >= 0 else "Loss",
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
    st.altair_chart(chart, use_container_width=True)


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
    return database.get_all_trades(broker_filter=broker_filter, account_filter=account_filter)


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
            if st.button("Pause", icon=":material/pause:", use_container_width=True):
                settings_manager.update_setting("bot_status", "PAUSED")
                st.rerun()
        else:
            if st.button("Resume", icon=":material/play_arrow:", use_container_width=True, type="primary"):
                settings_manager.update_setting("bot_status", "RUNNING")
                st.rerun()

        st.write("")
        if st.button("🚨 PANIC KILL-SWITCH", help="Immediately stops opening any new positions", use_container_width=True):
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
            )
            req_auth_time = st.checkbox(
                "Require Authoritative Time",
                value=bool(settings.get("require_authoritative_time", False)),
            )
            late_game_threshold = st.number_input(
                "Late Game Threshold (seconds)",
                min_value=60,
                max_value=3600,
                value=int(settings.get("late_game_threshold_seconds", 600)),
                step=30,
            )

        with sb_tab_risk:
            st.markdown("**Probability & Odds**")
            price_min = st.slider(
                "Min Price (Entry Floor)",
                min_value=0.85,
                max_value=0.995,
                value=float(settings.get("price_min", 0.97)),
                step=0.005,
                format="%.3f",
            )
            price_max = st.slider(
                "Max Price (Entry Ceiling)",
                min_value=0.95,
                max_value=0.999,
                value=float(settings.get("price_max", 0.995)),
                step=0.001,
                format="%.3f",
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
        saved = st.form_submit_button("💾 Save & Apply Config", use_container_width=True, type="primary")
        if saved:
            updated_settings = {
                "poll_interval_seconds": poll_interval,
                "require_healthy_data": req_healthy,
                "require_high_confidence": req_high_conf,
                "late_game_enabled": late_game_enabled,
                "require_authoritative_time": req_auth_time,
                "late_game_threshold_seconds": late_game_threshold,
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
    p_floor = float(settings.get("price_min", 0.97)) * 100
    p_ceil = float(settings.get("price_max", 0.995)) * 100
    daily_limit = int(settings.get("max_trades_per_day", 10))
    exposure_limit = float(settings.get("max_total_exposure", 200.0))
    cooldown = int(settings.get("poll_interval_seconds", 60))
    return [
        {"Rule": "Probability Floor Threshold", "Value": f"= {p_floor:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Probability Ceiling Threshold", "Value": f"= {p_ceil:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Daily Trades Limit", "Value": f"≤ {daily_limit}", "Status": "ENFORCED"},
        {"Rule": "Max Total Exposure", "Value": f"≤ ${exposure_limit:,.2f}", "Status": "ENFORCED"},
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
    # Scoped to the active mode to match summary() above -- otherwise this table would
    # show PAPER positions while the KPI ribbon above it reports LIVE-only numbers (or
    # vice versa).
    positions = {
        k: p for k, p in state.get("positions", {}).items()
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
        if st.button("SCAN NOW", icon=":material/radar:", type="primary", use_container_width=True, key="overview_scan_now"):
            settings_manager.update_setting("manual_scan_requested", True)
            with st.spinner("Scanning Polymarket sports markets..."):
                opps = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
                broker.save_signals(opps)
                broker.add_log(f"Manual scan completed: {len(opps)} opportunities found.")
            st.success(f"Scan complete! Found {len(opps)} signals.")
            st.rerun()

    # --- Metrics row ---
    with st.container(horizontal=True):
        st.metric("Net PnL", f"${summary.get('realized_pnl', 0.0):+,.2f}", f"{summary.get('closed_trades', 0)} settled trades", border=True)
        st.metric("Win Rate", f"{summary.get('win_rate', 0.0):.1f}%", f"{summary.get('wins', 0)}W / {summary.get('losses', 0)}L", border=True)
        st.metric("Open Exposure", f"${summary.get('open_exposure', 0.0):,.2f}", f"{summary.get('open_positions', 0)} of {settings.get('max_open_positions', 10)} positions", border=True)
        st.metric("Reserved Capital", f"${reserved_capital:,.2f}", border=True)

    st.write("")
    col_positions, col_gates = st.columns([2.4, 1])

    with col_positions:
        st.markdown("##### Open Positions")
        if not positions:
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
                    "Verify Trade": st.column_config.LinkColumn("Verify Trade", display_text="🔗 View ↗"),
                },
                hide_index=True,
            )
            with st.expander("🔍 1-Click Verification, Exit & Settlement"):
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

                            st.caption(f"Tokens: `{shares_held:.2f}` | Entry: `${entry_p:.4f}`")
                            if live_bid:
                                st.info(f"Live Best Bid on CLOB: **${live_bid:.4f}**")
                            else:
                                st.caption("No immediate bids in book; using entry price.")

                            exit_p = st.number_input(
                                "Exit Price ($)",
                                min_value=0.01,
                                max_value=1.00,
                                value=float(default_exit),
                                step=0.01,
                                key=f"ov_exit_p_{tid}",
                            )
                            est_return = shares_held * exit_p
                            est_pnl = est_return - invested
                            pnl_pct = (est_pnl / invested * 100.0) if invested > 0 else 0.0
                            pnl_color = "green" if est_pnl >= 0 else "red"
                            st.markdown(f"Est. Return: **${est_return:.2f}** | P&L: :{pnl_color}[**${est_pnl:+.2f} ({pnl_pct:+.1f}%)**]")

                            exit_btn_label = "Sell on CLOB" if pos_mode == "LIVE" else "Close Paper"
                            if st.button(exit_btn_label, icon=":material/point_of_sale:", type="primary", key=f"ov_btn_exit_{tid}", width="stretch"):
                                if pos_mode == "LIVE":
                                    live_inst = live_broker.get_live_broker()
                                    if not live_inst:
                                        st.error("Live broker not ready.")
                                        st.stop()
                                    try:
                                        with st.spinner(f"Submitting sell order on CLOB for {pos_acc}..."):
                                            live_inst.exit_position(pos_acc, token_id_str, size=shares_held, price=exit_p)
                                        broker.exit_position(tid, exit_price=exit_p, note=f"Manual Live Exit via Dashboard @ ${exit_p:.4f}")
                                        _cached_all_trades.clear()
                                        st.success(f"Sold on CLOB and settled position! PnL: ${est_pnl:+.2f}")
                                        st.rerun()
                                    except Exception as ex:
                                        st.error(f"Failed to exit on-chain: {ex}")
                                else:
                                    broker.exit_position(tid, exit_price=exit_p, note=f"Manual Paper Exit via Dashboard @ ${exit_p:.4f}")
                                    _cached_all_trades.clear()
                                    st.success(f"Closed paper trade! PnL: ${est_pnl:+.2f}")
                                    st.rerun()
                    with qc4:
                        pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                        with pop:
                            st.caption(f"Settle {p.get('question')[:30]}...")
                            if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"ov_won_{tid}", width="stretch"):
                                broker.force_settle_position(tid, won=True, note="Manual settlement via Dashboard: WON")
                                _cached_all_trades.clear()
                                st.success("Settled as WON!")
                                st.rerun()
                            if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"ov_lost_{tid}", width="stretch"):
                                broker.force_settle_position(tid, won=False, note="Manual settlement via Dashboard: LOST")
                                _cached_all_trades.clear()
                                st.warning("Settled as LOST.")
                                st.rerun()

    with col_gates:
        st.markdown("##### Circuit Breakers")
        st.dataframe(pd.DataFrame(build_gates_data())[["Rule", "Value"]], hide_index=True, use_container_width=True)

    st.write("")
    st.markdown("##### Live Market Signals")
    st.markdown('<div class="sst-section-sub">Opportunities passing your price, liquidity, and resolution-window filters right now.</div>', unsafe_allow_html=True)
    if not signals:
        st.info("No signals matching current threshold criteria in the latest scan. Click 'SCAN NOW' above or wait for the next loop.")
    else:
        df_signals = []
        for s in signals:
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
                "Polymarket": st.column_config.LinkColumn("Polymarket", display_text="🔗 View Market ↗"),
                "Price": st.column_config.NumberColumn("Price", format="$%.3f"),
                "Implied Win %": st.column_config.ProgressColumn("Implied Win %", format="%.1f%%", min_value=0, max_value=100),
                "24h Volume": st.column_config.NumberColumn("24h Volume", format="$%d"),
                "Liquidity": st.column_config.NumberColumn("Liquidity", format="$%d"),
            },
            hide_index=True,
        )

        with st.expander("🚀 Manual Trade Trigger", expanded=False):
            st.write(f"Execute a trade in **{execution_mode_str}** mode on one of the detected signals:")
            signal_options = {f"{s['question'][:60]} ({s['outcome_label']} @ {s['confirmed_price']:.3f})": s for s in signals}
            selected_signal_name = st.selectbox("Select Signal to Trade", list(signal_options.keys()), key="overview_signal_select")

            selected_sig = signal_options[selected_signal_name]
            sel_slug = selected_sig.get("slug") or database.resolve_market_slug(selected_sig.get("market_id"))
            sel_url = database.get_polymarket_url(sel_slug, selected_sig.get("market_id"))
            st.link_button(f"Inspect '{selected_sig.get('question')[:45]}...' on Polymarket", sel_url, icon=":material/open_in_new:")

            col_stake, col_btn = st.columns([2, 1])
            with col_stake:
                manual_stake = st.number_input(
                    "Trade Stake ($)",
                    min_value=1.0,
                    max_value=500.0,
                    value=float(settings.get("stake_per_trade", 25.0)),
                    step=5.0,
                    key="overview_manual_stake_input",
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
                        live_inst = live_broker.get_live_broker()
                        if live_inst:
                            try:
                                results = live_inst.place_buy_all(obj.token_id, obj.confirmed_price, override_stake=manual_stake)
                                any_success = False
                                for res in results:
                                    if res["success"]:
                                        any_success = True
                                        broker.open_position(
                                            obj,
                                            stake=res["stake"],
                                            mode="LIVE",
                                            account_name=res["account_name"],
                                            wallet_address=res["wallet"],
                                        )
                                        st.success(f"Live order placed for `{res['account_name']}` ({obj.question[:35]})!")
                                    else:
                                        st.error(f"Order failed for `{res['account_name']}`: {res['error']}")
                                if any_success:
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
            st.dataframe(pd.DataFrame(acc_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No trading accounts configured yet.")

    with col_limits:
        st.markdown("##### Strategy and Limits")
        limits_data = [
            {"Setting": "Min Price (Entry Floor)", "Value": f"{float(settings.get('price_min', 0.97)):.3f}"},
            {"Setting": "Min 24h Volume", "Value": f"${float(settings.get('min_volume', 5000.0)):,.0f}"},
            {"Setting": "Max Open Positions", "Value": str(int(settings.get("max_open_positions", 10)))},
            {"Setting": "Max Trades / Day", "Value": str(int(settings.get("max_trades_per_day", 10)))},
        ]
        st.dataframe(pd.DataFrame(limits_data), hide_index=True, use_container_width=True)

    st.write("")
    st.markdown("##### Full Circuit Breaker and Risk Gate Table")
    gates_df = pd.DataFrame(build_gates_data())
    st.dataframe(
        gates_df,
        hide_index=True,
        use_container_width=True,
        column_config={"Status": st.column_config.TextColumn("Status")},
    )

    st.write("")
    with st.expander("⚙️ Engine Controls (order limits & slippage)"):
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

    with st.expander("➕ Add / Manage Trading Accounts", expanded=not creds_ok):
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
            st.divider()
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
                        pac_price_min, pac_price_max = st.slider(
                            "Price Band", min_value=0.5, max_value=1.0,
                            value=(float(pac_settings.get("price_min") or 0.97), float(pac_settings.get("price_max") or 0.995)),
                            step=0.001, format="%.3f", key=f"pac_price_{pac_acc}",
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
                    st.dataframe(pd.DataFrame(all_orders), hide_index=True)

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
                        f"Update {vitals['name']} Stake ($)", min_value=1.0, max_value=1000.0, value=float(cur_single_stk), step=5.0,
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
                        st.dataframe(pd.DataFrame(live_orders), hide_index=True)

                    st.markdown(f"##### On-Chain Positions — `{vitals['name']}`")
                    live_pos = _cached_live_positions(sel_acc)
                    if not live_pos:
                        st.info(f"No open on-chain positions for {vitals['name']}.")
                    else:
                        st.dataframe(pd.DataFrame(live_pos), hide_index=True)
        else:
            st.warning("Could not initialize Live Broker instance. Check terminal logs for details.")

    with st.expander("🩺 Lifecycle State & Activity Logs"):
        with st.container(horizontal=True):
            st.metric("Dashboard State", status, delta="ACTIVE" if status == "RUNNING" else "HALTED", delta_color="normal" if status == "RUNNING" else "inverse", border=True)
            st.metric("Execution Mode", execution_mode_str, delta="MAINNET" if is_live else "SANDBOX", delta_color="normal" if is_live else "off", border=True)
            st.metric("System State Store", "CONNECTED", delta="SQLite & State JSON", border=True)
        logs = state.get("logs", [])
        if not logs:
            st.info("No activity logs recorded yet.")
        else:
            df_logs = [{"Time": item.get("timestamp", "")[:19].replace("T", " "), "Level": item.get("level", "INFO"), "Message": item.get("message", "")} for item in reversed(logs[-25:])]
            st.dataframe(pd.DataFrame(df_logs), hide_index=True)


# =============================================================
# SCREEN 3: HISTORY AND PERFORMANCE
# Trade history and realized performance (paper + live + on-chain).
# =============================================================
with tab_history:
    perf_options = ["Live Execution Portfolio (On-Chain)", "Paper Simulation Portfolio"]
    default_perf = perf_options[0] if is_live else perf_options[1]
    perf_portfolio_view = st.segmented_control("Select Portfolio View", perf_options, default=default_perf, key="hist_perf_view") or default_perf

    if perf_portfolio_view == "Live Execution Portfolio (On-Chain)":
        live_inst = live_broker.get_live_broker()
        if not live_inst:
            st.warning("Live trading credentials are not configured or invalid in `.env`. Check credentials in the **Control and Risk** screen.")
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
            closed_live = [t for t in live_trades if "PENDING" not in str(t.get("result", "")).upper()]
            wins_live = len([t for t in closed_live if float(t.get("pnl", 0.0)) > 0])
            realized_live = sum(float(t.get("pnl", 0.0)) for t in closed_live)
            win_rate_live = (wins_live / len(closed_live) * 100) if closed_live else 0.0
            avg_pnl_live = (realized_live / len(closed_live)) if closed_live else 0.0

            with st.container(horizontal=True):
                st.metric("Total Trades", len(live_trades), border=True)
                st.metric("Win Rate", f"{win_rate_live:.1f}%", f"{len(closed_live)} settled", border=True)
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
        closed = [t for t in trades_list if "PENDING" not in str(t.get("result", "")).upper()]
        wins = len([t for t in closed if float(t.get("pnl", 0.0)) > 0])
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
            with st.expander(f"⚡ Active Open Trades ({len(open_trades)} active)", expanded=False):
                st.caption("Inspect live odds on Polymarket or immediately settle completed matches:")
                for ot_idx, ot in enumerate(open_trades):
                    ot_slug = ot.get("slug") or database.resolve_market_slug(ot.get("market_id"))
                    ot_url = database.get_polymarket_url(ot_slug, ot.get("market_id"))
                    ot_tok = ot.get("token_id")
                    ot_key = f"{ot_idx}_{ot.get('trade_id') or ot_tok}"
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
                            st.caption(f"Tokens: `{ot_tokens:.2f}` | Entry: `${ot_entry:.4f}`")
                            if live_bid:
                                st.info(f"Live Best Bid on CLOB: **${live_bid:.4f}**")
                            else:
                                st.caption("No immediate bids in book; using entry price.")

                            exit_p = st.number_input(
                                "Exit Price ($)",
                                min_value=0.01,
                                max_value=1.00,
                                value=float(default_exit),
                                step=0.01,
                                key=f"h_exit_p_{ot_key}",
                            )
                            est_return = ot_tokens * exit_p
                            est_pnl = est_return - ot_cost
                            pnl_pct = (est_pnl / ot_cost * 100.0) if ot_cost > 0 else 0.0
                            pnl_color = "green" if est_pnl >= 0 else "red"
                            st.markdown(f"Est. Return: **${est_return:.2f}** | P&L: :{pnl_color}[**${est_pnl:+.2f} ({pnl_pct:+.1f}%)**]")

                            exit_btn_label = "Sell on CLOB" if ot_broker == "live" else "Close Paper"
                            if st.button(exit_btn_label, icon=":material/point_of_sale:", type="primary", key=f"h_btn_exit_{ot_key}", width="stretch"):
                                if ot_broker == "live":
                                    live_inst = live_broker.get_live_broker()
                                    if not live_inst:
                                        st.error("Live broker not ready.")
                                        st.stop()
                                    try:
                                        with st.spinner(f"Submitting sell order on CLOB for {ot_acc}..."):
                                            live_inst.exit_position(ot_acc, ot_tok_str, size=ot_tokens, price=exit_p)
                                        broker.exit_position(ot_tok, exit_price=exit_p, note=f"Manual Live Exit via History @ ${exit_p:.4f}")
                                        database.exit_orphaned_trade(ot.get("trade_id"), exit_price=exit_p, note=f"Manual Live Exit @ ${exit_p:.4f}")
                                        _cached_all_trades.clear()
                                        st.success(f"Sold on CLOB and settled position! PnL: ${est_pnl:+.2f}")
                                        st.rerun()
                                    except Exception as ex:
                                        st.error(f"Failed to exit on-chain: {ex}")
                                else:
                                    broker.exit_position(ot_tok, exit_price=exit_p, note=f"Manual Paper Exit via History @ ${exit_p:.4f}")
                                    database.exit_orphaned_trade(ot.get("trade_id"), exit_price=exit_p, note=f"Manual Paper Exit @ ${exit_p:.4f}")
                                    _cached_all_trades.clear()
                                    st.success(f"Closed trade! PnL: ${est_pnl:+.2f}")
                                    st.rerun()
                    with o_c4:
                        pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                        with pop:
                            st.caption(f"Settle {ot.get('question')[:30]}...")
                            if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"h_won_{ot_key}", width="stretch"):
                                broker.force_settle_position(ot_tok, won=True, note="Manual settlement via Dashboard: WON")
                                _cached_all_trades.clear()
                                st.success("Settled as WON!")
                                st.rerun()
                            if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"h_lost_{ot_key}", width="stretch"):
                                broker.force_settle_position(ot_tok, won=False, note="Manual settlement via Dashboard: LOST")
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
            column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="🔗 View Market ↗")},
            hide_index=True,
        )

    with st.expander("🔗 Live On-Chain Wallet Activity (Data API)"):
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
                    st.dataframe(pd.DataFrame(oc_positions), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="🔗 Verify ↗")}, hide_index=True)
            with oc_t2:
                if not oc_trades:
                    st.info(f"No recent filled trades found for {active_addr[:10]}...")
                else:
                    st.dataframe(pd.DataFrame(oc_trades), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="🔗 View ↗")}, hide_index=True)
            with oc_t3:
                if not oc_closed:
                    st.info(f"No historical settled positions found for {active_addr[:10]}...")
                else:
                    st.dataframe(pd.DataFrame(oc_closed), column_config={"Polymarket": st.column_config.LinkColumn("Polymarket", display_text="🔗 View ↗")}, hide_index=True)

    with st.expander("🔍 Orphaned Trade Reconciliation"):
        st.caption(
            "PENDING trades recorded in `trades.db` with no matching open position in "
            "`state.json` -- e.g. after a portfolio reset or a manual state.json edit that "
            "dropped a position the trade log still remembers. These rows are invisible to "
            "the Overview tab's Open Positions / Exposure (which read state.json only) but "
            "still show up under Active Open Trades below (which reads trades.db). Review "
            "each one and decide: restore it as a tracked open position, settle it as "
            "WON/LOST, or delete it as stale data."
        )
        all_pending = [t for t in database.get_all_trades(limit=1000) if str(t.get("result", "")).upper() == "PENDING"]
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

    with st.expander("🧹 Paper Portfolio Maintenance"):
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            if st.button("Reset State / Paper Portfolio", icon=":material/delete_forever:", help="Resets paper balance to $1,000 and clears paper positions (does NOT affect your live wallet)", width="stretch"):
                broker.state = {
                    "balance": config.STARTING_BALANCE,
                    "positions": {},
                    "closed_trades": [],
                    "signals": [],
                    "daily_trades": {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "count": 0},
                    "order_lifecycle": {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0},
                    "logs": [{"timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO", "message": "Portfolio reset by user."}],
                }
                broker.save()
                st.success("Paper portfolio reset successfully!")
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
    closed_collab = [t for t in collab_trades if "PENDING" not in str(t.get("result", "")).upper()]
    wins_collab = len([t for t in closed_collab if float(t.get("pnl", 0.0)) > 0])
    losses_collab = len(closed_collab) - wins_collab
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
        for t in collab_trades[-30:]:
            res = str(t.get("result", "PENDING")).upper()
            res_tag = "✅ WON" if "WON" in res else ("❌ LOST" if "LOST" in res else "⏳ PENDING")
            pnl_val = float(t.get("pnl") or 0.0)
            pnl_disp = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"
            condensed_rows.append({
                "Question": str(t.get("question", "")),
                "P&L $": pnl_disp,
                "Result": res_tag,
            })
        st.dataframe(pd.DataFrame(condensed_rows), hide_index=True, use_container_width=True)

    st.write("")
    st.markdown("##### Realized P&L, Last 10 Trades")
    with st.container(border=True):
        render_pnl_bar_chart(collab_trades)
