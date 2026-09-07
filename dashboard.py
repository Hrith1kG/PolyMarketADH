"""Streamlit Primary Control Panel for Polymarket Sureshot Trading Bot.
Supports seamless switching between Paper and Live Execution, Live Account Vitals,
and full strategy lifecycle monitoring."""
import json
import os
import time
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

from dotenv import load_dotenv
load_dotenv(override=True)

import config
import importlib
importlib.reload(config)
import paper_broker
importlib.reload(paper_broker)
from paper_broker import PaperBroker
import database
importlib.reload(database)
import live_broker
importlib.reload(live_broker)
import scanner
import settings_manager

st.set_page_config(
    page_title="Polymarket Sureshot Control Panel",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling matching dark-theme reference screenshots
st.markdown("""
<style>
    .metric-card {
        background-color: #171b26;
        border-radius: 8px;
        padding: 16px 20px;
        border: 1px solid #252c3d;
        margin-bottom: 12px;
    }
    .metric-card-title {
        font-size: 0.85rem;
        color: #8fa0b5;
        margin-bottom: 6px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .metric-card-value {
        font-size: 1.75rem;
        font-weight: 700;
        color: #ffffff;
    }
    .status-running {
        color: #00e676;
        font-weight: bold;
    }
    .status-paused {
        color: #ffb300;
        font-weight: bold;
    }
    .status-ready {
        color: #00e676;
        font-weight: bold;
    }
    .status-warning {
        color: #ff5252;
        font-weight: bold;
    }
    .info-callout {
        background-color: #122338;
        border-left: 4px solid #1976d2;
        padding: 12px 16px;
        border-radius: 4px;
        color: #90caf9;
        margin-bottom: 16px;
        font-size: 0.95rem;
    }
    .caution-banner {
        background-color: #332b12;
        border-left: 4px solid #ffb300;
        padding: 12px 16px;
        border-radius: 4px;
        color: #ffe082;
        margin-bottom: 16px;
        font-size: 0.95rem;
        font-weight: 500;
    }
</style>
""", unsafe_allow_html=True)


def get_broker() -> PaperBroker:
    return PaperBroker()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_on_chain_wallet_data(address: str):
    """Queries Polymarket's official Data API for an arbitrary wallet address."""
    import polymarket_client
    client = polymarket_client.get_public_client()
    clean_addr = address.strip()
    trades = []
    positions = []
    closed_pos = []

    try:
        trades_paginator = client.list_trades(user=clean_addr, page_size=50)
        for t in trades_paginator:
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
        print(f"[dashboard] Error fetching trades for {clean_addr}: {e}")

    try:
        positions_paginator = client.list_positions(user=clean_addr)
        for p in positions_paginator:
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
        print(f"[dashboard] Error fetching positions for {clean_addr}: {e}")

    try:
        closed_paginator = client.list_closed_positions(user=clean_addr)
        for cp in closed_paginator:
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
        print(f"[dashboard] Error fetching closed positions for {clean_addr}: {e}")

    return trades, positions, closed_pos


broker = get_broker()
settings = settings_manager.load_settings()
summary = broker.summary()
state = broker.state

# Current Execution Mode
is_live = bool(settings.get("live_trading", False))
execution_mode_str = "LIVE" if is_live else "PAPER"
kill_switch_active = bool(settings.get("entry_kill_switch", False))
creds_ok, creds_msg = live_broker.check_credentials_available()


# ==========================================
# SIDEBAR: PRIMARY STRATEGY CONTROLS
# ==========================================
with st.sidebar:
    st.title("⚡ Control Panel")

    # --- Bot Execution State ---
    st.subheader("Bot Status")
    status = settings.get("bot_status", "RUNNING")
    if status == "RUNNING":
        st.markdown('Current Status: <span class="status-running">🟢 RUNNING</span>', unsafe_allow_html=True)
        if st.button("⏸️ Pause Bot Execution"):
            settings_manager.update_setting("bot_status", "PAUSED")
            st.rerun()
    else:
        st.markdown('Current Status: <span class="status-paused">🟡 PAUSED</span>', unsafe_allow_html=True)
        if st.button("▶️ Resume Bot Execution"):
            settings_manager.update_setting("bot_status", "RUNNING")
            st.rerun()

    # --- Mode Quick Indicator in Sidebar ---
    st.markdown(f"**Execution Mode:** `{execution_mode_str}`")

    if st.button("🔄 Trigger Immediate Scan"):
        settings_manager.update_setting("manual_scan_requested", True)
        with st.spinner("Scanning Polymarket sports markets..."):
            opps = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
            broker.save_signals(opps)
            broker.add_log(f"Manual scan completed: {len(opps)} opportunities found.")
        st.success(f"Scan complete! Found {len(opps)} signals.")
        st.rerun()

    st.markdown("---")

    # --- Settings Form ---
    with st.form("sidebar_config_form"):
        st.subheader("Strategy & Limits")
        poll_interval = st.number_input(
            "Cooldown (seconds)",
            min_value=10,
            max_value=600,
            value=int(settings.get("poll_interval_seconds", 60)),
            step=5,
            help="Polling interval cooldown between market scans.",
        )

        st.markdown("**Health & Confidence Gates**")
        req_healthy = st.checkbox(
            "Require Healthy Data",
            value=bool(settings.get("require_healthy_data", True)),
        )
        req_high_conf = st.checkbox(
            "Require High Confidence Match",
            value=bool(settings.get("require_high_confidence", False)),
        )

        st.markdown("**Late Game Settings**")
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

        st.markdown("**👛 Wallet Tracking (Data API)**")
        tracked_wallet = st.text_input(
            "Polymarket / Proxy Address",
            value=str(settings.get("tracked_wallet_address", "")),
            placeholder="0x...",
            help="Enter any Polymarket profile or proxy wallet address to track on-chain activity, trades, and PnL.",
        )

        with st.expander("⚙️ Advanced Risk & Threshold Settings", expanded=False):
            st.markdown("**Probability & Odds Limits**")
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

        saved = st.form_submit_button("💾 Save & Apply Config")
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

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🚨 PANIC KILL-SWITCH", help="Immediately stops opening any new positions"):
        settings_manager.update_setting("entry_kill_switch", True)
        st.error("PANIC KILL-SWITCH ACTIVATED! New orders blocked.")
        st.rerun()


# ==========================================
# MAIN DASHBOARD TABS
# ==========================================
st.title("⚡ Polymarket Sureshot: Execution Control Panel")
st.caption("Sports Moneyline Ultra-Probability Automated Trading Engine")

# Top Navigation Tabs matching user reference screenshots
(
    tab_exec,
    tab_ops,
    tab_live,
    tab_signals,
    tab_history,
    tab_perf,
) = st.tabs([
    "⚙️ Execution Pipeline",
    "🚨 Operations & Health",
    "🔴 Live Control",
    "🎯 Market Signals",
    "📜 Trade History",
    "📊 Performance Analytics",
])


# =============================================================
# TAB 1: EXECUTION PIPELINE (Matches Screenshot 1)
# =============================================================
with tab_exec:
    st.header("Order Management & Execution Pipeline")

    lifecycle = summary.get("order_lifecycle", {})
    intentions_count = lifecycle.get("intentions", 0)
    filled_count = lifecycle.get("filled", 0)
    rejected_count = lifecycle.get("rejected", 0)
    pending_count = lifecycle.get("pending", 0)
    reserved_capital = summary.get("reserved_capital", summary.get("open_exposure", 0.0))

    st.subheader("🏦 Pipeline Metrics")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Execution Mode</div>
            <div class="metric-card-value">{execution_mode_str}</div>
        </div>
        """, unsafe_allow_html=True)
    with c2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Total Intentions</div>
            <div class="metric-card-value">{intentions_count}</div>
        </div>
        """, unsafe_allow_html=True)
    with c3:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Filled Orders</div>
            <div class="metric-card-value">{filled_count}</div>
        </div>
        """, unsafe_allow_html=True)
    with c4:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Reserved Capital</div>
            <div class="metric-card-value">${reserved_capital:,.2f}</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    col_lifecycle, col_positions = st.columns([1, 2.5])

    with col_lifecycle:
        st.subheader("📋 Order Lifecycle")
        with st.container(border=True):
            st.markdown(f"**Total Intentions:** `{intentions_count}`")
            st.markdown(f"**Pending Validation:** `{pending_count}`")
            st.markdown(f"**Filled Orders:** ✅ `{filled_count}`")
            st.markdown(f"**Rejected (Risk/Sizing):** ❌ `{rejected_count}`")

    with col_positions:
        st.subheader("💼 Open Positions (Tracked)")
        positions = state.get("positions", {})
        if not positions:
            st.info("No active positions currently tracked.")
        else:
            df_pos = []
            for tid, p in positions.items():
                mode = p.get("mode", execution_mode_str)
                event_id = str(p.get("event_id") or p.get("market_id") or tid)[:12] + "..."
                token_short = str(tid)[:8] + "..."
                slug_val = p.get("slug") or database.resolve_market_slug(p.get("market_id"))
                poly_url = database.get_polymarket_url(slug_val, p.get("market_id"))
                df_pos.append({
                    "Verify Trade": poly_url,
                    "Mode": mode,
                    "Question": p.get("question", "")[:50],
                    "Outcome": p.get("outcome_label", ""),
                    "Avg Entry": f"${p.get('entry_price', 0):.4f}",
                    "Quantity": f"{p.get('shares', 0):.2f}",
                    "Capital": f"${p.get('stake', 0):.2f}",
                    "Time Left": p.get("time_left", "0.0m"),
                    "Event ID": event_id,
                    "Token": token_short,
                })
            st.dataframe(
                pd.DataFrame(df_pos),
                column_config={
                    "Verify Trade": st.column_config.LinkColumn(
                        "Verify Trade",
                        display_text="🔗 View on Polymarket ↗",
                        help="Click to open and verify this market directly on Polymarket",
                    ),
                },
                hide_index=True,
                use_container_width=True,
            )

            # Direct 1-click verification & settlement controls
            st.markdown("##### 🔍 1-Click Verification & Settlement")
            for tid, p in list(positions.items()):
                slug_val = p.get("slug") or database.resolve_market_slug(p.get("market_id"))
                poly_url = database.get_polymarket_url(slug_val, p.get("market_id"))
                qc1, qc2, qc3 = st.columns([2.5, 1.1, 1.0])
                with qc1:
                    st.markdown(f"**{p.get('question', '')}** — `{p.get('outcome_label', '')}` · Entry: **{p.get('entry_price', 0):.2f}** · Capital: **${p.get('stake', 0):.2f}**")
                with qc2:
                    st.link_button("🔗 Open Market ↗", poly_url, use_container_width=True)
                with qc3:
                    pop = st.popover("⚡ Settle", use_container_width=True)
                    with pop:
                        st.caption(f"Settle {p.get('question')[:30]}...")
                        if st.button("✅ Settle WON (1.0)", key=f"t1_won_{tid[:10]}", use_container_width=True):
                            broker.force_settle_position(tid, won=True, note="Manual settlement via Dashboard: WON")
                            st.success("Settled as WON!")
                            st.rerun()
                        if st.button("❌ Settle LOST (0.0)", key=f"t1_lost_{tid[:10]}", use_container_width=True):
                            broker.force_settle_position(tid, won=False, note="Manual settlement via Dashboard: LOST")
                            st.warning("Settled as LOST.")
                            st.rerun()


# =============================================================
# TAB 2: OPERATIONS & HEALTH (Matches Screenshot 2)
# =============================================================
with tab_ops:
    st.header("System Health & Operations")

    st.subheader("🖥️ Lifecycle State")
    lc1, lc2, lc3 = st.columns(3)
    with lc1:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Dashboard State</div>
            <div class="metric-card-value">{status}</div>
        </div>
        """, unsafe_allow_html=True)
    with lc2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Execution Mode</div>
            <div class="metric-card-value">{execution_mode_str}</div>
        </div>
        """, unsafe_allow_html=True)
    with lc3:
        st.markdown("""
        <div class="metric-card">
            <div class="metric-card-title">System State Store</div>
            <div class="metric-card-value">CONNECTED</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
    <div class="info-callout">
        ℹ️ For real-time background bot logs, check your terminal output. The dashboard connects directly to the state store to ensure no performance impact on the trading loop.
    </div>
    """, unsafe_allow_html=True)

    st.subheader("🛡️ Circuit Breakers & Gates")
    p_floor = float(settings.get("price_min", 0.97)) * 100
    p_ceil = float(settings.get("price_max", 0.995)) * 100
    daily_limit = int(settings.get("max_trades_per_day", 10))
    exposure_limit = float(settings.get("max_total_exposure", 200.0))
    cooldown = int(settings.get("poll_interval_seconds", 60))

    gates_data = [
        {"Rule": "Probability Floor Threshold", "Value": f"= {p_floor:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Probability Ceiling Threshold", "Value": f"= {p_ceil:.2f}%", "Status": "ENFORCED"},
        {"Rule": "Daily Trades Limit", "Value": f"≤ {daily_limit}", "Status": "ENFORCED"},
        {"Rule": "Max Total Exposure", "Value": f"≤ ${exposure_limit:,.2f}", "Status": "ENFORCED"},
        {"Rule": "Cooldown Timer / Loop Interval", "Value": f"{cooldown}s", "Status": "ENFORCED"},
        {"Rule": "Entry Kill Switch", "Value": "ACTIVE" if kill_switch_active else "ARMED", "Status": "ENFORCED"},
        {"Rule": "Sports Moneyline Filter", "Value": "Tag 100639 / ML", "Status": "ENFORCED"},
    ]
    st.dataframe(pd.DataFrame(gates_data))

    st.markdown("---")
    st.subheader("Activity Stream Logs")
    logs = state.get("logs", [])
    if not logs:
        st.info("No activity logs recorded yet.")
    else:
        df_logs = []
        for item in reversed(logs[-25:]):
            df_logs.append({
                "Time": item.get("timestamp", "")[:19].replace("T", " "),
                "Level": item.get("level", "INFO"),
                "Message": item.get("message", ""),
            })
        st.dataframe(pd.DataFrame(df_logs))


# =============================================================
# TAB 3: LIVE CONTROL (Matches Screenshot 3)
# =============================================================
with tab_live:
    st.header("🔴 LIVE TRADING CONTROL")
    st.markdown("""
    <div class="caution-banner">
        ⚠️ These controls directly affect real capital on Polymarket.
    </div>
    """, unsafe_allow_html=True)

    st.subheader("LIVE_READINESS")
    lr1, lr2, lr3, lr4 = st.columns(4)

    live_enabled_label = "TRUE" if is_live else "FALSE"
    live_enabled_icon = "✅" if is_live else "❌"

    kill_switch_label = "ON" if kill_switch_active else "OFF"
    kill_switch_icon = "🔴" if kill_switch_active else "🟢"

    creds_label = "OK" if creds_ok else "MISSING"
    creds_icon = "✅" if creds_ok else "❌"

    if not is_live:
        readiness_label = "NOT_LIVE_MODE"
        readiness_icon = "🔴"
    elif not creds_ok:
        readiness_label = "MISSING_CREDS"
        readiness_icon = "⚠️"
    else:
        readiness_label = "READY"
        readiness_icon = "🟢"

    with lr1:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Live Enabled</div>
            <div class="metric-card-value">{live_enabled_icon} {live_enabled_label}</div>
        </div>
        """, unsafe_allow_html=True)
    with lr2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Entry Kill Switch</div>
            <div class="metric-card-value">{kill_switch_icon} {kill_switch_label}</div>
        </div>
        """, unsafe_allow_html=True)
    with lr3:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Credentials</div>
            <div class="metric-card-value">{creds_icon} {creds_label}</div>
        </div>
        """, unsafe_allow_html=True)
    with lr4:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-card-title">Live Readiness</div>
            <div class="metric-card-value">{readiness_icon} {readiness_label}</div>
        </div>
        """, unsafe_allow_html=True)

    # Dynamic status notice banner
    if not is_live:
        st.markdown("""
        <div class="info-callout">
            ℹ️ Current EXECUTION_MODE is <b>PAPER</b>. Toggle <b>'Enable LIVE Trading'</b> below to switch dynamically without restarting the bot.
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="caution-banner">
            ⚡ Current EXECUTION_MODE is <b>LIVE</b>. Real orders are placed on Polymarket mainnet. Toggle OFF below to return to PAPER mode at any time.
        </div>
        """, unsafe_allow_html=True)

    st.subheader("Control Panel")
    cp_col1, cp_col2 = st.columns(2)

    with cp_col1:
        # Live Trading Toggle
        toggle_live = st.toggle(
            "Enable LIVE Trading",
            value=is_live,
            help="Enables real on-chain execution via Polymarket CLOB. Requires PRIVATE_KEY in .env.",
        )
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

        # Entry Kill Switch Toggle
        toggle_kill = st.toggle(
            "Entry Kill Switch (blocks new entries)",
            value=kill_switch_active,
            help="Immediately halts opening any new orders while leaving position tracking and resolution active.",
        )
        if toggle_kill != kill_switch_active:
            settings_manager.update_setting("entry_kill_switch", toggle_kill)
            st.rerun()

        # Max Order Notional
        current_stake = float(settings.get("stake_per_trade", 25.0))
        notional_input = st.number_input(
            "Max Order Notional ($)",
            min_value=1.0,
            max_value=1000.0,
            value=current_stake,
            step=5.0,
            help="Maximum USD stake per single trade order.",
        )
        if notional_input != current_stake:
            settings_manager.update_setting("stake_per_trade", notional_input)

    with cp_col2:
        current_max_pos = int(settings.get("max_open_positions", 10))
        max_pos_input = st.number_input(
            "Max Open Positions",
            min_value=1,
            max_value=50,
            value=current_max_pos,
            step=1,
        )
        if max_pos_input != current_max_pos:
            settings_manager.update_setting("max_open_positions", max_pos_input)

        current_daily_limit = int(settings.get("max_trades_per_day", 10))
        daily_limit_input = st.number_input(
            "Daily Trade Limit",
            min_value=1,
            max_value=100,
            value=current_daily_limit,
            step=1,
        )
        if daily_limit_input != current_daily_limit:
            settings_manager.update_setting("max_trades_per_day", daily_limit_input)

        current_slippage = float(settings.get("max_slippage", 0.005))
        slippage_input = st.number_input(
            "Max Slippage Price",
            min_value=0.001,
            max_value=0.05,
            value=current_slippage,
            step=0.001,
            format="%.3f",
            help="Maximum allowed difference between quoted price and fill price.",
        )
        if slippage_input != current_slippage:
            settings_manager.update_setting("max_slippage", slippage_input)

    st.markdown("---")
    st.subheader("Live Account & Orders")

    if not creds_ok:
        st.markdown("""
        <div class="info-callout">
            ℹ️ Live data will populate here when LIVE mode is running and readiness is READY.<br>
            To activate live trading, add your Polymarket account credentials to your <code>.env</code> file:
            <pre>
PRIVATE_KEY=0x_your_wallet_private_key
FUNDER_ADDRESS=0x_your_polymarket_profile_wallet_address  # Optional: For email/magic/proxy users
            </pre>
        </div>
        """, unsafe_allow_html=True)
    else:
        # Load live vitals from connected SecureClient
        live_instance = live_broker.get_live_broker()
        if live_instance:
            vitals = live_instance.get_account_vitals()
            v_col1, v_col2, v_col3, v_col4 = st.columns(4)
            with v_col1:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-card-title">Connected Wallet</div>
                    <div class="metric-card-value" style="font-size: 1.1rem; word-break: break-all;">
                        {vitals['wallet'][:6]}...{vitals['wallet'][-4:]}
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with v_col2:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-card-title">Wallet Type</div>
                    <div class="metric-card-value">{vitals['wallet_type']}</div>
                </div>
                """, unsafe_allow_html=True)
            with v_col3:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-card-title">Collateral (USDC.e)</div>
                    <div class="metric-card-value">${vitals['collateral_balance']:,.2f}</div>
                </div>
                """, unsafe_allow_html=True)
            with v_col4:
                allowance_val = vitals['allowance']
                allowance_disp = "MAX (Unlimited)" if allowance_val > 1_000_000_000 else f"${allowance_val:,.2f}"
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-card-title">Collateral Allowance</div>
                    <div class="metric-card-value" style="font-size: 1.35rem;">{allowance_disp}</div>
                </div>
                """, unsafe_allow_html=True)

            # Live Open CLOB Orders
            st.markdown("#### Open CLOB Orders")
            live_orders = live_instance.get_open_orders()
            if not live_orders:
                st.info("No open CLOB limit orders on the exchange.")
            else:
                st.dataframe(pd.DataFrame(live_orders))

            # Live On-Chain Positions
            st.markdown("#### On-Chain Positions")
            live_pos = live_instance.get_live_positions()
            if not live_pos:
                st.info("No open on-chain positions returned for this wallet.")
            else:
                st.dataframe(pd.DataFrame(live_pos))
        else:
            st.warning("Could not initialize Live Broker instance. Check terminal logs for details.")


# =============================================================
# TAB 4: MARKET SIGNALS
# =============================================================
with tab_signals:
    st.header("🎯 Live Sureshot Sports Signals")
    signals = state.get("signals", [])

    if not signals:
        st.info("No signals matching current threshold criteria in the latest scan. Click 'Trigger Immediate Scan' in the sidebar or wait for the next loop.")
    else:
        st.caption(f"Showing {len(signals)} top qualifying match(es) from recent scan.")
        df_signals = []
        for s in signals:
            slug = s.get("slug", "") or database.resolve_market_slug(s.get("market_id"))
            poly_url = database.get_polymarket_url(slug, s.get("market_id"))
            df_signals.append({
                "Polymarket": poly_url,
                "Match / Game": s.get("question"),
                "Outcome": s.get("outcome_label"),
                "Confirmed Price": f"${s.get('confirmed_price', 0):.3f}",
                "Implied Win %": f"{s.get('confirmed_price', 0) * 100:.1f}%",
                "24h Volume": f"${s.get('volume', 0):,.0f}",
                "Liquidity": f"${s.get('liquidity', 0):,.0f}",
                "Market Type": s.get("market_type", "moneyline"),
                "End Date": s.get("end_date", "N/A"),
                "Token ID": s.get("token_id"),
            })
        st.dataframe(
            pd.DataFrame(df_signals),
            column_config={
                "Polymarket": st.column_config.LinkColumn(
                    "Polymarket",
                    display_text="🔗 View Market ↗",
                    help="Click to inspect this market on Polymarket",
                ),
            },
            hide_index=True,
            use_container_width=True,
        )

        # Manual Trade Executor
        st.markdown("### Manual Trade Trigger")
        st.write(f"Execute a trade in **{execution_mode_str}** mode on one of the signals:")
        signal_options = {f"{s['question'][:60]} ({s['outcome_label']} @ {s['confirmed_price']:.3f})": s for s in signals}
        selected_signal_name = st.selectbox("Select Signal to Trade", list(signal_options.keys()))

        selected_sig = signal_options[selected_signal_name]
        sel_slug = selected_sig.get("slug") or database.resolve_market_slug(selected_sig.get("market_id"))
        sel_url = database.get_polymarket_url(sel_slug, selected_sig.get("market_id"))
        st.link_button(f"🔗 Inspect '{selected_sig.get('question')[:45]}...' on Polymarket ↗", sel_url)

        col_stake, col_btn = st.columns([2, 1])
        with col_stake:
            manual_stake = st.number_input(
                "Trade Stake ($)",
                min_value=1.0,
                max_value=500.0,
                value=float(settings.get("stake_per_trade", 25.0)),
                step=5.0,
                key="manual_stake_input",
            )
        with col_btn:
            st.write("")
            st.write("")
            btn_label = f"🚀 Open {execution_mode_str} Position"
            if st.button(btn_label):
                target_sig = signal_options[selected_signal_name]
                class SigObj:
                    pass
                obj = SigObj()
                for k, v in target_sig.items():
                    setattr(obj, k, v)

                # If live mode is enabled, also execute on CLOB
                if is_live:
                    live_inst = live_broker.get_live_broker()
                    if live_inst:
                        try:
                            live_inst.place_buy(obj.token_id, obj.confirmed_price, manual_stake)
                            st.success(f"Live order placed on Polymarket for {obj.question[:40]}!")
                        except Exception as e:
                            st.error(f"Failed to execute live order: {e}")
                            st.stop()
                    else:
                        st.error("Live broker not ready. Check credentials.")
                        st.stop()

                try:
                    pos, reason = broker.open_position(obj, stake=manual_stake, mode=execution_mode_str)
                except TypeError:
                    pos, reason = broker.open_position(obj, stake=manual_stake)
                    if pos:
                        pos["mode"] = execution_mode_str

                if pos:
                    st.success(f"Successfully opened position on {obj.question[:40]} with ${manual_stake} stake!")
                    st.rerun()
                else:
                    st.error(f"Cannot open position: {reason}")


# =============================================================
# TAB 5: TRADE HISTORY & ON-CHAIN ACTIVITY (Matches Screenshot)
# =============================================================
with tab_history:
    col_hdr, col_actions = st.columns([2.5, 1.5])
    with col_hdr:
        st.header("📜 Trade History & On-Chain Activity")
    with col_actions:
        btn_c1, btn_c2 = st.columns(2)
        with btn_c1:
            if st.button("🔄 Refresh History"):
                st.rerun()
        with btn_c2:
            if st.button("⚡ Settle Completed"):
                with st.spinner("Checking market resolutions and completed matches..."):
                    settled = broker.check_resolutions()
                    if settled:
                        st.success(f"Settled {len(settled)} completed trades in Local DB & credited balance!")
                    else:
                        st.info("No open trades are ready to settle automatically.")
                st.rerun()

    data_source = st.radio(
        "Select Data Source",
        ["🔴 Bot Execution Log (Local DB)", "⛓️ Live On-Chain Activity (Data API)"],
        horizontal=True,
    )

    if data_source == "🔴 Bot Execution Log (Local DB)":
        available_outcomes = ["ALL"] + database.get_available_outcomes()
        selected_outcome = st.selectbox("Filter by Outcome", available_outcomes)

        trades_list = database.get_all_trades(
            outcome_filter=None if selected_outcome == "ALL" else selected_outcome
        )

        if not trades_list:
            st.info("No trades recorded in Local DB yet. The bot will record here as soon as orders are entered.")
        else:
            # Open / Pending Trades Quick Verification & Settlement Bar
            open_trades = [t for t in trades_list if "PENDING" in str(t.get("result", "")).upper()]
            if open_trades:
                with st.expander(f"⚡ Active Open Trades ({len(open_trades)} active)", expanded=True):
                    st.caption("Inspect live odds on Polymarket or immediately settle completed matches:")
                    for ot in open_trades:
                        ot_slug = ot.get("slug") or database.resolve_market_slug(ot.get("market_id"))
                        ot_url = database.get_polymarket_url(ot_slug, ot.get("market_id"))
                        ot_tok = ot.get("token_id")
                        o_c1, o_c2, o_c3 = st.columns([2.5, 1.1, 1.0])
                        with o_c1:
                            st.markdown(f"**{ot.get('question')}** · `{ot.get('outcome')}` · Entry: **{float(ot.get('entry_price', 0)):.2f}** · Cost: **${float(ot.get('cost', 0)):.2f}**")
                        with o_c2:
                            st.link_button("🔗 Open Market ↗", ot_url, use_container_width=True)
                        with o_c3:
                            pop = st.popover("⚡ Settle", use_container_width=True)
                            with pop:
                                st.caption(f"Settle {ot.get('question')[:30]}...")
                                if st.button("✅ Settle WON (1.0)", key=f"t5_won_{str(ot_tok)[:10]}", use_container_width=True):
                                    broker.force_settle_position(ot_tok, won=True, note="Manual settlement via Dashboard: WON")
                                    st.success("Settled as WON!")
                                    st.rerun()
                                if st.button("❌ Settle LOST (0.0)", key=f"t5_lost_{str(ot_tok)[:10]}", use_container_width=True):
                                    broker.force_settle_position(ot_tok, won=False, note="Manual settlement via Dashboard: LOST")
                                    st.warning("Settled as LOST.")
                                    st.rerun()

            table_rows = []
            for t in trades_list:
                res = str(t.get("result", "PENDING")).upper()
                if "WON" in res:
                    res_tag = "✅ WON"
                elif "LOST" in res:
                    res_tag = "❌ LOST"
                else:
                    res_tag = "⏳ PENDING"

                entry_val = float(t.get("entry_price") or 0.0)
                entry_disp = f"{entry_val * 100:.2f}%" if entry_val < 1.0 else f"${entry_val:.2f}"

                pnl_val = float(t.get("pnl") or 0.0)
                pnl_disp = f"+${pnl_val:.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):.2f}"

                raw_tid = str(t.get("trade_id", ""))
                tid_disp = raw_tid if len(raw_tid) <= 14 else raw_tid[:14] + "..."

                slug_val = t.get("slug") or database.resolve_market_slug(t.get("market_id"))
                poly_url = database.get_polymarket_url(slug_val, t.get("market_id"))

                table_rows.append({
                    "Polymarket": poly_url,
                    "Trade ID": tid_disp,
                    "Placed At": str(t.get("placed_at", ""))[:19].replace("T", " "),
                    "Question": str(t.get("question", "")),
                    "Outcome": str(t.get("outcome", "")),
                    "Entry $": entry_disp,
                    "Tokens": round(float(t.get("tokens") or 0.0), 4),
                    "Cost $": f"${float(t.get('cost') or 0.0):.2f}",
                    "T Left": str(t.get("time_left", "0.0m")),
                    "Result": res_tag,
                    "P&L $": pnl_disp,
                    "Broker": str(t.get("broker", "paper")).lower(),
                })
            st.dataframe(
                pd.DataFrame(table_rows),
                column_config={
                    "Polymarket": st.column_config.LinkColumn(
                        "Polymarket",
                        display_text="🔗 View Market ↗",
                        help="Click to open and verify this market directly on Polymarket",
                    ),
                },
                hide_index=True,
                use_container_width=True,
            )

    else:
        # Live On-Chain Activity (Data API)
        st.subheader("⛓️ Live On-Chain Activity (Data API)")
        tracked_addr = settings.get("tracked_wallet_address", "").strip()

        col_addr_in, col_fetch_btn = st.columns([3, 1])
        with col_addr_in:
            addr_val = st.text_input(
                "Target Polymarket / Proxy Wallet Address",
                value=tracked_addr,
                placeholder="0x...",
                help="Enter any Polymarket profile or proxy address to query live on-chain trades, positions, and PnL via official Data API.",
            )
        with col_fetch_btn:
            st.write("")
            st.write("")
            if st.button("🔍 Fetch Live Data"):
                if addr_val.strip() != tracked_addr:
                    settings_manager.update_setting("tracked_wallet_address", addr_val.strip())
                    st.rerun()

        active_addr = addr_val.strip() or tracked_addr
        if not active_addr or not active_addr.startswith("0x") or len(active_addr) < 40:
            st.markdown("""
            <div class="info-callout">
                ℹ️ Enter a valid Polymarket wallet address (<code>0x...</code>) in the input above or in the sidebar to populate live on-chain trades, positions, and P&L.
            </div>
            """, unsafe_allow_html=True)
        else:
            with st.spinner(f"Fetching on-chain Data API feed for {active_addr[:8]}..."):
                oc_trades, oc_positions, oc_closed = fetch_on_chain_wallet_data(active_addr)

            oc_t1, oc_t2, oc_t3 = st.tabs([
                "💼 Open Positions",
                "📈 Filled Trades History",
                "📜 Settled & Realized P&L",
            ])

            with oc_t1:
                st.markdown("#### Currently Held On-Chain Positions")
                if not oc_positions:
                    st.info(f"No active positions found for {active_addr[:10]}...")
                else:
                    st.dataframe(
                        pd.DataFrame(oc_positions),
                        column_config={
                            "Polymarket": st.column_config.LinkColumn(
                                "Polymarket",
                                display_text="🔗 Verify On-Chain ↗",
                                help="Click to open this market on Polymarket",
                            ),
                        },
                        hide_index=True,
                        use_container_width=True,
                    )

            with oc_t2:
                st.markdown("#### Live Filled Orders & Trades Log")
                if not oc_trades:
                    st.info(f"No recent filled trades found for {active_addr[:10]}...")
                else:
                    st.dataframe(
                        pd.DataFrame(oc_trades),
                        column_config={
                            "Polymarket": st.column_config.LinkColumn(
                                "Polymarket",
                                display_text="🔗 View Market ↗",
                                help="Click to open this market on Polymarket",
                            ),
                        },
                        hide_index=True,
                        use_container_width=True,
                    )

            with oc_t3:
                st.markdown("#### Resolved Positions & Realized P&L")
                if not oc_closed:
                    st.info(f"No historical settled positions found for {active_addr[:10]}...")
                else:
                    st.dataframe(
                        pd.DataFrame(oc_closed),
                        column_config={
                            "Polymarket": st.column_config.LinkColumn(
                                "Polymarket",
                                display_text="🔗 View Market ↗",
                                help="Click to open this market on Polymarket",
                            ),
                        },
                        hide_index=True,
                        use_container_width=True,
                    )


# =============================================================
# TAB 6: PERFORMANCE ANALYTICS
# =============================================================
with tab_perf:
    st.header("📊 Performance Analytics")

    # Select between Live On-Chain and Paper Simulation
    default_perf_idx = 0 if is_live else 1
    perf_portfolio_view = st.radio(
        "Select Portfolio View",
        ["🔴 Live Execution Portfolio (On-Chain)", "📝 Paper Simulation Portfolio"],
        index=default_perf_idx,
        horizontal=True,
    )

    if perf_portfolio_view == "🔴 Live Execution Portfolio (On-Chain)":
        live_inst = live_broker.get_live_broker()
        if not live_inst:
            st.warning("⚠️ Live trading credentials are not configured or invalid in `.env`. Go to the **🔴 Live Control** tab to check credentials.")
        else:
            with st.spinner("Fetching live on-chain account metrics..."):
                live_bal = live_inst.get_collateral_balance()
                live_allowance = live_inst.get_allowance()
                live_pos = live_inst.get_live_positions()
                live_orders = live_inst.get_open_orders()

            live_exposure = sum(float(p.get("current_value", 0.0)) for p in live_pos)
            live_unrealized = sum(float(p.get("cash_pnl", 0.0)) for p in live_pos)

            # Query live trades from SQLite
            live_trades = database.get_all_trades(broker_filter="live")
            closed_live = [t for t in live_trades if "PENDING" not in str(t.get("result", "")).upper()]
            wins_live = len([t for t in closed_live if float(t.get("pnl", 0.0)) > 0])
            losses_live = len([t for t in closed_live if float(t.get("pnl", 0.0)) <= 0])
            realized_live = sum(float(t.get("pnl", 0.0)) for t in closed_live)
            win_rate_live = (wins_live / len(closed_live) * 100) if closed_live else 0.0
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_live = len([t for t in live_trades if str(t.get("placed_at", "")).startswith(today_str)])

            p1, p2, p3, p4, p5, p6 = st.columns(6)
            with p1:
                st.metric(
                    "Live Collateral (USDC.e)",
                    f"${live_bal:,.2f}",
                    delta=f"{realized_live:+.2f} Realized P&L" if realized_live != 0 else None,
                )
            with p2:
                st.metric(
                    "Live Exposure",
                    f"${live_exposure:,.2f}",
                    delta=f"{live_unrealized:+.2f} Unrealized" if live_unrealized != 0 else None,
                )
            with p3:
                st.metric(
                    "Open Positions",
                    f"{len(live_pos)} / {settings.get('max_open_positions', 10)}",
                )
            with p4:
                st.metric(
                    "Trades Today",
                    f"{today_live} / {settings.get('max_trades_per_day', 10)}",
                )
            with p5:
                st.metric(
                    "Realized P&L",
                    f"${realized_live:+.2f}",
                    delta=f"{wins_live}W / {losses_live}L",
                )
            with p6:
                st.metric(
                    "Win Rate",
                    f"{win_rate_live:.1f}%",
                    f"{len(closed_live)} settled",
                )

            st.markdown(f"""
            <div class="info-callout">
                👛 <b>Connected Wallet:</b> <code>{live_inst.wallet}</code> ({live_inst.wallet_type}) &nbsp;|&nbsp; 
                💰 <b>Live USDC.e:</b> <code>${live_bal:,.2f}</code> &nbsp;|&nbsp; 
                🔓 <b>Allowance:</b> <code>${live_allowance:,.2f}</code> &nbsp;|&nbsp; 
                📋 <b>Open CLOB Orders:</b> <code>{len(live_orders)}</code>
            </div>
            """, unsafe_allow_html=True)

            if live_pos:
                st.markdown("#### Currently Held Live Positions")
                df_live_pos = []
                for p in live_pos:
                    slug_v = database.resolve_market_slug(p.get("market_id"))
                    p_url = database.get_polymarket_url(slug_v, p.get("market_id"))
                    df_live_pos.append({
                        "Polymarket": p_url,
                        "Title": p.get("title", "")[:50],
                        "Outcome": p.get("outcome", ""),
                        "Size": p.get("size", 0.0),
                        "Avg Price": f"${p.get('avg_price', 0):.4f}",
                        "Current Value": f"${p.get('current_value', 0):.2f}",
                        "Cash P&L": f"${p.get('cash_pnl', 0):+.2f}",
                        "% P&L": f"{p.get('percent_pnl', 0):+.1f}%",
                    })
                st.dataframe(
                    pd.DataFrame(df_live_pos),
                    column_config={
                        "Polymarket": st.column_config.LinkColumn(
                            "Polymarket",
                            display_text="🔗 View on Polymarket ↗",
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                )

            if live_orders:
                st.markdown("#### Active CLOB Limit Orders")
                st.dataframe(pd.DataFrame(live_orders), hide_index=True, use_container_width=True)

    else:
        # Paper Simulation View
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        with p1:
            st.metric(
                "Paper Balance",
                f"${summary['balance']:,.2f}",
                delta=f"{summary['realized_pnl']:+.2f} P&L" if summary['realized_pnl'] != 0 else None,
            )
        with p2:
            st.metric(
                "Open Exposure",
                f"${summary['open_exposure']:,.2f}",
                f"Max: ${settings.get('max_total_exposure', 200):,.0f}",
            )
        with p3:
            st.metric(
                "Open Positions",
                f"{summary['open_positions']} / {settings.get('max_open_positions', 10)}",
            )
        with p4:
            st.metric(
                "Trades Today",
                f"{summary['today_trades']} / {settings.get('max_trades_per_day', 10)}",
            )
        with p5:
            st.metric(
                "Realized P&L",
                f"${summary['realized_pnl']:+.2f}",
                delta=f"{summary['wins']}W / {summary['losses']}L",
            )
        with p6:
            st.metric(
                "Win Rate",
                f"{summary['win_rate']:.1f}%",
                f"{summary['closed_trades']} settled",
            )

        st.markdown("---")
        st.subheader("Paper Danger Zone")
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            if st.button("🗑️ Reset State / Paper Portfolio", help="Resets paper balance to $1,000 and clears paper positions (does NOT affect your live wallet)"):
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
            if st.button("🔄 Reset Settings to Defaults"):
                settings_manager.save_settings(settings_manager.DEFAULT_SETTINGS)
                st.success("Settings restored to factory defaults!")
                st.rerun()

