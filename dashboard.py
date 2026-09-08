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
    page_title="Polymarket Sureshot Terminal",
    page_icon=":material/bolt:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Terminal dark-mode typography & metric card polish
st.markdown("""
<style>
    div[data-testid="stMetric"] {
        background: #111622;
        border: 1px solid #1E293B;
        border-radius: 8px;
        padding: 12px 16px;
    }
    div[data-testid="stMetricLabel"] p {
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: #94A3B8;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.6rem;
        font-weight: 700;
        color: #F8FAFC;
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
    st.markdown("### :material/tune: Control Panel")

    with st.container(border=True):
        st.caption("Engine Status & Mode")
        status = settings.get("bot_status", "RUNNING")
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
            if st.button("Pause Bot", icon=":material/pause:"):
                settings_manager.update_setting("bot_status", "PAUSED")
                st.rerun()
        else:
            if st.button("Resume Bot", icon=":material/play_arrow:"):
                settings_manager.update_setting("bot_status", "RUNNING")
                st.rerun()

        if st.button("Trigger Scan Now", icon=":material/radar:"):
            settings_manager.update_setting("manual_scan_requested", True)
            with st.spinner("Scanning Polymarket sports markets..."):
                opps = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
                broker.save_signals(opps)
                broker.add_log(f"Manual scan completed: {len(opps)} opportunities found.")
            st.success(f"Scan complete! Found {len(opps)} signals.")
            st.rerun()


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
st.title(":material/bolt: Polymarket Sureshot Terminal")
st.caption("Institutional-Grade Ultra-Probability Sports Moneyline Execution Engine")

# Top Navigation Tabs matching user reference screenshots
(
    tab_exec,
    tab_ops,
    tab_live,
    tab_signals,
    tab_history,
    tab_perf,
) = st.tabs([
    ":material/sync_alt: Execution Pipeline",
    ":material/health_and_safety: Operations & Health",
    ":material/tune: Live Control",
    ":material/radar: Market Signals",
    ":material/history: Trade History",
    ":material/analytics: Performance Analytics",
])


# =============================================================
# TAB 1: EXECUTION PIPELINE
# =============================================================
with tab_exec:
    st.header("Order Management & Execution Pipeline")

    lifecycle = summary.get("order_lifecycle", {})
    intentions_count = lifecycle.get("intentions", 0)
    filled_count = lifecycle.get("filled", 0)
    rejected_count = lifecycle.get("rejected", 0)
    pending_count = lifecycle.get("pending", 0)
    reserved_capital = summary.get("reserved_capital", summary.get("open_exposure", 0.0))

    st.subheader("Pipeline Metrics")
    with st.container(horizontal=True):
        st.metric(
            "Execution Mode",
            execution_mode_str,
            delta="REAL CAPITAL" if is_live else "SIMULATED",
            delta_color="normal" if is_live else "off",
            border=True,
        )
        st.metric(
            "Total Intentions",
            intentions_count,
            help="Total trade opportunities evaluated by execution engine",
            border=True,
        )
        st.metric(
            "Filled Orders",
            filled_count,
            delta=f"{rejected_count} rejected" if rejected_count else "0 rejected",
            delta_color="inverse" if rejected_count else "off",
            border=True,
        )
        st.metric(
            "Reserved Capital",
            f"${reserved_capital:,.2f}",
            border=True,
        )

    st.write("")

    col_lifecycle, col_positions = st.columns([1, 2.5])

    with col_lifecycle:
        st.subheader("Order Lifecycle")
        with st.container(border=True):
            st.metric("Total Intentions", intentions_count)
            st.metric("Pending Validation", pending_count)
            st.metric("Filled Orders", filled_count)
            st.metric("Rejected (Risk/Sizing)", rejected_count)

    with col_positions:
        st.subheader("Open Positions (Tracked)")
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
                    "Account": p.get("account_name", "Primary"),
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
            )

            # Direct 1-click verification & settlement controls
            st.markdown("##### 🔍 1-Click Verification & Settlement")
            for tid, p in list(positions.items()):
                slug_val = p.get("slug") or database.resolve_market_slug(p.get("market_id"))
                poly_url = database.get_polymarket_url(slug_val, p.get("market_id"))
                qc1, qc2, qc3 = st.columns([2.5, 1.1, 1.0])
                with qc1:
                    acc_lbl = f"[{p.get('account_name', 'Primary')}] " if p.get("account_name") else ""
                    st.markdown(f"**{acc_lbl}{p.get('question', '')}** — `{p.get('outcome_label', '')}` · Entry: **{p.get('entry_price', 0):.2f}** · Capital: **${p.get('stake', 0):.2f}**")
                with qc2:
                    st.link_button("Open Market", poly_url, icon=":material/open_in_new:", width="stretch")
                with qc3:
                    pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                    with pop:
                        st.caption(f"Settle {p.get('question')[:30]}...")
                        if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"t1_won_{tid[:10]}", width="stretch"):
                            broker.force_settle_position(tid, won=True, note="Manual settlement via Dashboard: WON")
                            st.success("Settled as WON!")
                            st.rerun()
                        if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"t1_lost_{tid[:10]}", width="stretch"):
                            broker.force_settle_position(tid, won=False, note="Manual settlement via Dashboard: LOST")
                            st.warning("Settled as LOST.")
                            st.rerun()


# =============================================================
# TAB 2: OPERATIONS & HEALTH
# =============================================================
with tab_ops:
    st.header("System Health & Operations")

    st.subheader("Lifecycle State")
    with st.container(horizontal=True):
        st.metric(
            "Dashboard State",
            status,
            delta="ACTIVE" if status == "RUNNING" else "HALTED",
            delta_color="normal" if status == "RUNNING" else "inverse",
            border=True,
        )
        st.metric(
            "Execution Mode",
            execution_mode_str,
            delta="MAINNET" if is_live else "SANDBOX",
            delta_color="normal" if is_live else "off",
            border=True,
        )
        st.metric(
            "System State Store",
            "CONNECTED",
            delta="SQLite & State JSON",
            delta_color="normal",
            border=True,
        )

    st.info("Real-time background execution loop logs are output directly to terminal / NSSM service logs. The dashboard connects directly to the state store to ensure zero latency impact on the trading loop.")

    st.subheader("Circuit Breakers & Risk Gates")
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
    st.dataframe(pd.DataFrame(gates_data), hide_index=True)

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
        st.dataframe(pd.DataFrame(df_logs), hide_index=True)


# =============================================================
# TAB 3: LIVE CONTROL
# =============================================================
with tab_live:
    st.header("Live Trading Control")
    st.warning("These controls directly affect real capital on Polymarket mainnet.")

    st.subheader("Live Execution Readiness")
    live_enabled_label = "TRUE" if is_live else "FALSE"
    kill_switch_label = "ACTIVE" if kill_switch_active else "OFF"
    creds_label = "CONFIGURED" if creds_ok else "MISSING"

    if not is_live:
        readiness_label = "PAPER_MODE"
    elif not creds_ok:
        readiness_label = "MISSING_CREDS"
    else:
        readiness_label = "LIVE_READY"

    with st.container(horizontal=True):
        st.metric(
            "Live Mode",
            live_enabled_label,
            delta="ACTIVE" if is_live else "DISABLED",
            delta_color="normal" if is_live else "off",
            border=True,
        )
        st.metric(
            "Entry Kill Switch",
            kill_switch_label,
            delta="BLOCKED" if kill_switch_active else "NORMAL",
            delta_color="inverse" if kill_switch_active else "normal",
            border=True,
        )
        st.metric(
            "Credentials",
            creds_label,
            delta="VERIFIED" if creds_ok else "MISSING",
            delta_color="normal" if creds_ok else "inverse",
            border=True,
        )
        st.metric(
            "Live Readiness",
            readiness_label,
            delta="READY" if readiness_label == "LIVE_READY" else "INACTIVE",
            delta_color="normal" if readiness_label == "LIVE_READY" else "off",
            border=True,
        )

    # Dynamic status notice banner
    if not is_live:
        st.info("Current EXECUTION_MODE is **PAPER**. Toggle **'Enable LIVE Trading'** below to switch dynamically without restarting the bot.")
    else:
        st.warning("Current EXECUTION_MODE is **LIVE**. Real orders are placed on Polymarket mainnet. Toggle OFF below to return to PAPER mode at any time.")

    st.subheader("Engine Controls")
    with st.container(border=True):
        cp_col1, cp_col2 = st.columns(2)

        with cp_col1:
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

            toggle_kill = st.toggle(
                "Entry Kill Switch (blocks new entries)",
                value=kill_switch_active,
                help="Immediately halts opening any new orders while leaving position tracking and resolution active.",
            )
            if toggle_kill != kill_switch_active:
                settings_manager.update_setting("entry_kill_switch", toggle_kill)
                st.rerun()

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

    st.subheader("Live Accounts & Orders")

    if not creds_ok:
        st.info("""
Live data will populate here when LIVE mode is running and readiness is READY.
To activate live trading, add your Polymarket account credentials to your `.env` file:
```bash
# Single-Account format:
PRIVATE_KEY=0x_your_wallet_private_key
FUNDER_ADDRESS=0x_your_polymarket_profile_wallet_address  # Optional

# Multi-Account format:
ACCOUNT_1_NAME=MetaMask_Main
ACCOUNT_1_PRIVATE_KEY=0x_first_private_key
ACCOUNT_1_FUNDER_ADDRESS=
ACCOUNT_1_STAKE=25.0

ACCOUNT_2_NAME=Secondary_Safe
ACCOUNT_2_PRIVATE_KEY=0x_second_private_key
ACCOUNT_2_FUNDER_ADDRESS=
ACCOUNT_2_STAKE=15.0
```
""")
    else:
        # Load live vitals from connected SecureClient
        live_instance = live_broker.get_live_broker()
        if live_instance:
            acc_names = live_instance.get_account_names()
            acc_filter_choices = ["Combined (All Accounts)"] + acc_names
            sel_tab3_acc = st.segmented_control(
                "Select Account View",
                acc_filter_choices,
                default="Combined (All Accounts)",
            ) or "Combined (All Accounts)"

            if sel_tab3_acc == "Combined (All Accounts)":
                agg = live_instance.get_aggregated_vitals()
                all_orders = live_instance.get_open_orders()
                all_pos = live_instance.get_live_positions()

                with st.container(horizontal=True):
                    st.metric("Active Accounts", agg['account_count'], border=True)
                    st.metric("Pooled Collateral (USDC.e)", f"${agg['total_collateral']:,.2f}", border=True)
                    st.metric("Total Open Orders", len(all_orders), border=True)
                    st.metric("Total Live Positions", len(all_pos), border=True)

                st.subheader("Connected Accounts Breakdown")
                breakdown_rows = []
                for a in agg["accounts"]:
                    allow_val = a["allowance"]
                    allow_disp = "MAX" if allow_val > 1_000_000_000 else f"${allow_val:,.2f}"
                    act_stk = settings_manager.get_account_stake(a["name"], fallback=a.get("stake", config.STAKE_PER_TRADE))
                    breakdown_rows.append({
                        "Account": a["name"],
                        "Wallet": a["wallet"],
                        "Type": a["wallet_type"],
                        "Collateral (USDC.e)": f"${a['collateral_balance']:,.2f}",
                        "Allowance": allow_disp,
                        "Stake Per Trade": f"${act_stk:.2f}",
                    })
                st.dataframe(pd.DataFrame(breakdown_rows), hide_index=True)

                if len(agg["accounts"]) > 1:
                    st.markdown("##### ⚡ Dynamic Per-Account Stake Sizing")
                    st.caption("Change trade sizing for any account instantly without editing `.env` or restarting the bot:")
                    stk_cols = st.columns(len(agg["accounts"]))
                    for idx, a in enumerate(agg["accounts"]):
                        acc_n = a["name"]
                        cur_acc_stk = settings_manager.get_account_stake(acc_n, fallback=a.get("stake", config.STAKE_PER_TRADE))
                        with stk_cols[idx]:
                            new_val = st.number_input(
                                f"{acc_n} Stake ($)",
                                min_value=1.0,
                                max_value=1000.0,
                                value=float(cur_acc_stk),
                                step=5.0,
                                key=f"tab3_acc_stk_{acc_n}",
                                help=f"Trade stake in USD specifically for {acc_n}",
                            )
                            if new_val != cur_acc_stk:
                                settings_manager.set_account_stake(acc_n, new_val)
                                st.success(f"Updated {acc_n} stake to ${new_val:.2f}!")
                                st.rerun()

                if len(agg["accounts"]) >= 1:
                    st.markdown("##### 🎛️ Per-Account Independent Controls")
                    st.caption("Each account can run, pause, kill-switch, risk-cap, and filter markets completely independently of the others and of the global bot controls.")
                    pac_acc = st.selectbox("Configure Account", acc_names, key="pac_select")
                    pac_settings = settings_manager.get_account_settings(pac_acc)
                    pac_overrides = settings_manager.load_settings().get("account_overrides", {}).get(pac_acc, {})

                    pac_col1, pac_col2 = st.columns(2)
                    with pac_col1:
                        pac_status = st.selectbox(
                            "Status",
                            ["RUNNING", "PAUSED"],
                            index=0 if pac_settings.get("bot_status", "RUNNING") == "RUNNING" else 1,
                            key=f"pac_status_{pac_acc}",
                            help="Pauses new entries for this account only; other accounts keep trading.",
                        )
                        pac_kill = st.toggle(
                            "Kill Switch",
                            value=bool(pac_settings.get("entry_kill_switch", False)),
                            key=f"pac_kill_{pac_acc}",
                            help="Blocks all new entries for this account only.",
                        )
                        pac_max_pos = st.number_input(
                            "Max Open Positions", min_value=1, max_value=200,
                            value=int(pac_settings.get("max_open_positions") or 10),
                            key=f"pac_maxpos_{pac_acc}",
                        )
                        pac_max_exp = st.number_input(
                            "Max Total Exposure ($)", min_value=1.0,
                            value=float(pac_settings.get("max_total_exposure") or 200.0),
                            key=f"pac_maxexp_{pac_acc}",
                        )
                        pac_max_trades = st.number_input(
                            "Max Trades / Day", min_value=1, max_value=500,
                            value=int(pac_settings.get("max_trades_per_day") or 10),
                            key=f"pac_maxtrades_{pac_acc}",
                        )
                    with pac_col2:
                        pac_price_min, pac_price_max = st.slider(
                            "Price Band",
                            min_value=0.5, max_value=1.0,
                            value=(float(pac_settings.get("price_min") or 0.97), float(pac_settings.get("price_max") or 0.995)),
                            step=0.001, format="%.3f",
                            key=f"pac_price_{pac_acc}",
                        )
                        pac_min_vol = st.number_input(
                            "Min Volume ($)", min_value=0.0,
                            value=float(pac_settings.get("min_volume") if pac_settings.get("min_volume") is not None else 5000.0),
                            key=f"pac_minvol_{pac_acc}",
                        )
                        pac_min_liq = st.number_input(
                            "Min Liquidity ($)", min_value=0.0,
                            value=float(pac_settings.get("min_liquidity") if pac_settings.get("min_liquidity") is not None else 1000.0),
                            key=f"pac_minliq_{pac_acc}",
                        )
                        pac_sports_types = st.multiselect(
                            "Sports Market Types",
                            ["moneyline", "spread", "totals"],
                            default=pac_settings.get("sports_market_types") or ["moneyline"],
                            key=f"pac_sports_{pac_acc}",
                        )

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

                # Combined Open Orders
                st.subheader("Open CLOB Orders (All Accounts)")
                if not all_orders:
                    st.info("No open CLOB limit orders across any account.")
                else:
                    st.dataframe(pd.DataFrame(all_orders), hide_index=True)

                # Combined Live Positions
                st.subheader("On-Chain Positions (All Accounts)")
                if not all_pos:
                    st.info("No open on-chain positions returned across accounts.")
                else:
                    st.dataframe(pd.DataFrame(all_pos), hide_index=True)

            else:
                # Single Account View
                session = live_instance.get_session(sel_tab3_acc)
                if session:
                    vitals = session.get_account_vitals()
                    allowance_val = vitals['allowance']
                    allowance_disp = "MAX (Unlimited)" if allowance_val > 1_000_000_000 else f"${allowance_val:,.2f}"

                    with st.container(horizontal=True):
                        st.metric("Connected Wallet", f"{vitals['wallet'][:6]}...{vitals['wallet'][-4:]}", help=vitals['wallet'], border=True)
                        st.metric("Wallet Type", vitals['wallet_type'], border=True)
                        st.metric("Collateral (USDC.e)", f"${vitals['collateral_balance']:,.2f}", border=True)
                        st.metric("Stake Per Trade", f"${vitals['stake']:.2f}", border=True)

                    with st.container(border=True):
                        cur_single_stk = settings_manager.get_account_stake(vitals["name"], fallback=vitals.get("stake", config.STAKE_PER_TRADE))
                        new_single_stk = st.number_input(
                            f"Update {vitals['name']} Stake ($)",
                            min_value=1.0,
                            max_value=1000.0,
                            value=float(cur_single_stk),
                            step=5.0,
                            key=f"single_acc_stk_{vitals['name']}",
                            help=f"Dynamically update stake for {vitals['name']} without restarting the bot.",
                        )
                        if new_single_stk != cur_single_stk:
                            settings_manager.set_account_stake(vitals["name"], new_single_stk)
                            st.success(f"Updated {vitals['name']} stake to ${new_single_stk:.2f}!")
                            st.rerun()

                    st.subheader(f"Open CLOB Orders — `{vitals['name']}`")
                    live_orders = session.get_open_orders()
                    if not live_orders:
                        st.info(f"No open CLOB limit orders for {vitals['name']}.")
                    else:
                        st.dataframe(pd.DataFrame(live_orders), hide_index=True)

                    st.subheader(f"On-Chain Positions — `{vitals['name']}`")
                    live_pos = session.get_live_positions()
                    if not live_pos:
                        st.info(f"No open on-chain positions for {vitals['name']}.")
                    else:
                        st.dataframe(pd.DataFrame(live_pos), hide_index=True)
        else:
            st.warning("Could not initialize Live Broker instance. Check terminal logs for details.")



# =============================================================
# TAB 4: MARKET SIGNALS
# =============================================================
with tab_signals:
    st.header("Live Sureshot Sports Signals")
    signals = state.get("signals", [])

    if not signals:
        st.info("No signals matching current threshold criteria in the latest scan. Click 'Trigger Scan Now' in the sidebar or wait for the next loop.")
    else:
        st.caption(f"Showing {len(signals)} top qualifying match(es) from recent scan.")
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
                "Price": st.column_config.NumberColumn(
                    "Price",
                    format="$%.3f",
                ),
                "Implied Win %": st.column_config.ProgressColumn(
                    "Implied Win %",
                    help="Estimated winning probability based on orderbook pricing",
                    format="%.1f%%",
                    min_value=0,
                    max_value=100,
                ),
                "24h Volume": st.column_config.NumberColumn(
                    "24h Volume",
                    format="$%d",
                ),
                "Liquidity": st.column_config.NumberColumn(
                    "Liquidity",
                    format="$%d",
                ),
            },
            hide_index=True,
        )

        # Manual Trade Executor
        st.subheader("Manual Trade Trigger")
        with st.container(border=True):
            st.write(f"Execute a trade in **{execution_mode_str}** mode on one of the detected signals:")
            signal_options = {f"{s['question'][:60]} ({s['outcome_label']} @ {s['confirmed_price']:.3f})": s for s in signals}
            selected_signal_name = st.selectbox("Select Signal to Trade", list(signal_options.keys()))

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
                    key="manual_stake_input",
                )
            with col_btn:
                st.write("")
                st.write("")
                btn_label = f"Open {execution_mode_str} Position"
                if st.button(btn_label, icon=":material/rocket_launch:", width="stretch"):
                    target_sig = signal_options[selected_signal_name]
                    class SigObj:
                        pass
                    obj = SigObj()
                    for k, v in target_sig.items():
                        setattr(obj, k, v)

                    # If live mode is enabled, execute across configured accounts on CLOB
                    if is_live:
                        live_inst = live_broker.get_live_broker()
                        if live_inst:
                            try:
                                results = live_inst.place_buy_all(obj.token_id, obj.confirmed_price, default_stake=manual_stake)
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
                            st.success(f"Successfully opened paper position on {obj.question[:40]} with ${manual_stake} stake!")
                            st.rerun()
                        else:
                            st.error(f"Cannot open position: {reason}")



# =============================================================
# TAB 5: TRADE HISTORY & ON-CHAIN ACTIVITY
# =============================================================
with tab_history:
    col_hdr, col_actions = st.columns([2.5, 1.5])
    with col_hdr:
        st.header("Trade History & On-Chain Activity")
    with col_actions:
        btn_c1, btn_c2 = st.columns(2)
        with btn_c1:
            if st.button("Refresh", icon=":material/refresh:", width="stretch"):
                st.rerun()
        with btn_c2:
            if st.button("Settle Completed", icon=":material/done_all:", width="stretch"):
                with st.spinner("Checking market resolutions and completed matches..."):
                    settled = broker.check_resolutions()
                    if settled:
                        st.success(f"Settled {len(settled)} completed trades in Local DB & credited balance!")
                    else:
                        st.info("No open trades are ready to settle automatically.")
                st.rerun()

    data_source = st.segmented_control(
        "Select Data Source",
        ["Bot Execution Log (Local DB)", "Live On-Chain Activity (Data API)"],
        default="Bot Execution Log (Local DB)",
    ) or "Bot Execution Log (Local DB)"

    if data_source == "Bot Execution Log (Local DB)":
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            available_outcomes = ["ALL"] + database.get_available_outcomes()
            selected_outcome = st.selectbox("Filter by Outcome", available_outcomes)
        with col_f2:
            available_accounts = ["ALL"] + database.get_available_accounts()
            selected_account = st.selectbox("Filter by Account", available_accounts)

        trades_list = database.get_all_trades(
            outcome_filter=None if selected_outcome == "ALL" else selected_outcome,
            account_filter=None if selected_account == "ALL" else selected_account,
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
                        acc_tag = f"[{ot.get('account_name', 'Primary')}] " if ot.get("account_name") else ""
                        o_c1, o_c2, o_c3 = st.columns([2.5, 1.1, 1.0])
                        with o_c1:
                            st.markdown(f"**{acc_tag}{ot.get('question')}** · `{ot.get('outcome')}` · Entry: **{float(ot.get('entry_price', 0)):.2f}** · Cost: **${float(ot.get('cost', 0)):.2f}**")
                        with o_c2:
                            st.link_button("Open Market", ot_url, icon=":material/open_in_new:", width="stretch")
                        with o_c3:
                            pop = st.popover("Settle", icon=":material/gavel:", width="stretch")
                            with pop:
                                st.caption(f"Settle {ot.get('question')[:30]}...")
                                if st.button("Settle WON (1.0)", icon=":material/check_circle:", key=f"t5_won_{str(ot_tok)[:10]}", width="stretch"):
                                    broker.force_settle_position(ot_tok, won=True, note="Manual settlement via Dashboard: WON")
                                    st.success("Settled as WON!")
                                    st.rerun()
                                if st.button("Settle LOST (0.0)", icon=":material/cancel:", key=f"t5_lost_{str(ot_tok)[:10]}", width="stretch"):
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
                    "Account": str(t.get("account_name", "Primary")),
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
            )


    else:
        # Live On-Chain Activity (Data API)
        st.subheader("Live On-Chain Activity (Data API)")
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
            if st.button("Fetch Live Data", icon=":material/search:", width="stretch"):
                if addr_val.strip() != tracked_addr:
                    settings_manager.update_setting("tracked_wallet_address", addr_val.strip())
                    st.rerun()

        active_addr = addr_val.strip() or tracked_addr
        if not active_addr or not active_addr.startswith("0x") or len(active_addr) < 40:
            st.info("Enter a valid Polymarket wallet address (`0x...`) in the input above or in the sidebar to populate live on-chain trades, positions, and P&L.")
        else:
            with st.spinner(f"Fetching on-chain Data API feed for {active_addr[:8]}..."):
                oc_trades, oc_positions, oc_closed = fetch_on_chain_wallet_data(active_addr)

            oc_t1, oc_t2, oc_t3 = st.tabs([
                ":material/work: Open Positions",
                ":material/trending_up: Filled Trades History",
                ":material/receipt_long: Settled & Realized P&L",
            ])

            with oc_t1:
                st.subheader("Currently Held On-Chain Positions")
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
                    )

            with oc_t2:
                st.subheader("Live Filled Orders & Trades Log")
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
                    )

            with oc_t3:
                st.subheader("Resolved Positions & Realized P&L")
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
                    )


# =============================================================
# TAB 6: PERFORMANCE ANALYTICS
# =============================================================
with tab_perf:
    st.header("Performance Analytics")

    # Select between Live On-Chain and Paper Simulation
    perf_options = ["Live Execution Portfolio (On-Chain)", "Paper Simulation Portfolio"]
    default_perf = perf_options[0] if is_live else perf_options[1]
    perf_portfolio_view = st.segmented_control(
        "Select Portfolio View",
        perf_options,
        default=default_perf,
    ) or default_perf

    if perf_portfolio_view == "Live Execution Portfolio (On-Chain)":
        live_inst = live_broker.get_live_broker()
        if not live_inst:
            st.warning("Live trading credentials are not configured or invalid in `.env`. Check credentials in the **Live Control** tab.")
        else:
            live_acc_choices = ["All Accounts (Combined)"] + live_inst.get_account_names()
            selected_perf_acc = st.segmented_control(
                "Portfolio Account Filter",
                live_acc_choices,
                default="All Accounts (Combined)",
            ) or "All Accounts (Combined)"

            is_combined = selected_perf_acc == "All Accounts (Combined)"
            target_acc = None if is_combined else selected_perf_acc

            with st.spinner("Fetching live on-chain account metrics..."):
                live_bal = live_inst.get_collateral_balance(account_name=target_acc)
                live_allowance = live_inst.get_allowance(account_name=target_acc)
                live_pos = live_inst.get_live_positions(account_name=target_acc)
                live_orders = live_inst.get_open_orders(account_name=target_acc)

            live_exposure = sum(float(p.get("current_value", 0.0)) for p in live_pos)
            live_unrealized = sum(float(p.get("cash_pnl", 0.0)) for p in live_pos)

            # Query live trades from SQLite for selected account
            live_trades = database.get_all_trades(broker_filter="live", account_filter=target_acc)
            closed_live = [t for t in live_trades if "PENDING" not in str(t.get("result", "")).upper()]
            wins_live = len([t for t in closed_live if float(t.get("pnl", 0.0)) > 0])
            losses_live = len([t for t in closed_live if float(t.get("pnl", 0.0)) <= 0])
            realized_live = sum(float(t.get("pnl", 0.0)) for t in closed_live)
            win_rate_live = (wins_live / len(closed_live) * 100) if closed_live else 0.0
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_live = len([t for t in live_trades if str(t.get("placed_at", "")).startswith(today_str)])

            lbl_bal = "Pooled Collateral" if is_combined else f"Collateral ({selected_perf_acc})"
            with st.container(horizontal=True):
                st.metric(
                    lbl_bal,
                    f"${live_bal:,.2f}",
                    delta=f"{realized_live:+.2f} Realized P&L" if realized_live != 0 else None,
                    border=True,
                )
                st.metric(
                    "Live Exposure",
                    f"${live_exposure:,.2f}",
                    delta=f"{live_unrealized:+.2f} Unrealized" if live_unrealized != 0 else None,
                    border=True,
                )
                st.metric(
                    "Open Positions",
                    f"{len(live_pos)} / {settings.get('max_open_positions', 10)}",
                    border=True,
                )
                st.metric(
                    "Trades Today",
                    f"{today_live} / {settings.get('max_trades_per_day', 10)}",
                    border=True,
                )
                st.metric(
                    "Realized P&L",
                    f"${realized_live:+.2f}",
                    delta=f"{wins_live}W / {losses_live}L",
                    border=True,
                )
                st.metric(
                    "Win Rate",
                    f"{win_rate_live:.1f}%",
                    f"{len(closed_live)} settled",
                    border=True,
                )

            # Info callout
            if is_combined:
                agg = live_inst.get_aggregated_vitals()
                st.info(f"**Configured Accounts:** `{agg['account_count']} active` · **Pooled USDC.e:** `${live_bal:,.2f}` · **Total Open CLOB Orders:** `{len(live_orders)}` · **Total Live Positions:** `{len(live_pos)}`")
            else:
                sess = live_inst.get_session(target_acc)
                sess_wallet = sess.wallet if sess else live_inst.wallet
                sess_wtype = sess.wallet_type if sess else live_inst.wallet_type
                st.info(f"**Account:** `{selected_perf_acc}` · **Wallet:** `{sess_wallet}` ({sess_wtype}) · **Live USDC.e:** `${live_bal:,.2f}` · **Allowance:** `${live_allowance:,.2f}` · **Open CLOB Orders:** `{len(live_orders)}`")

            if live_pos:
                st.subheader("Currently Held Live Positions")
                df_live_pos = []
                for p in live_pos:
                    slug_v = database.resolve_market_slug(p.get("market_id"))
                    p_url = database.get_polymarket_url(slug_v, p.get("market_id"))
                    row_data = {
                        "Polymarket": p_url,
                    }
                    if is_combined and p.get("account"):
                        row_data["Account"] = p.get("account")
                    row_data.update({
                        "Title": p.get("title", "")[:50],
                        "Outcome": p.get("outcome", ""),
                        "Size": p.get("size", 0.0),
                        "Avg Price": f"${p.get('avg_price', 0):.4f}",
                        "Current Value": f"${p.get('current_value', 0):.2f}",
                        "Cash P&L": f"${p.get('cash_pnl', 0):+.2f}",
                        "% P&L": f"{p.get('percent_pnl', 0):+.1f}%",
                    })
                    df_live_pos.append(row_data)
                st.dataframe(
                    pd.DataFrame(df_live_pos),
                    column_config={
                        "Polymarket": st.column_config.LinkColumn(
                            "Polymarket",
                            display_text="🔗 View on Polymarket ↗",
                        ),
                    },
                    hide_index=True,
                )

            if live_orders:
                st.subheader("Active CLOB Limit Orders")
                st.dataframe(pd.DataFrame(live_orders), hide_index=True)


    else:
        # Paper Simulation View
        with st.container(horizontal=True):
            st.metric(
                "Paper Balance",
                f"${summary['balance']:,.2f}",
                delta=f"{summary['realized_pnl']:+.2f} P&L" if summary['realized_pnl'] != 0 else None,
                border=True,
            )
            st.metric(
                "Open Exposure",
                f"${summary['open_exposure']:,.2f}",
                f"Max: ${settings.get('max_total_exposure', 200):,.0f}",
                border=True,
            )
            st.metric(
                "Open Positions",
                f"{summary['open_positions']} / {settings.get('max_open_positions', 10)}",
                border=True,
            )
            st.metric(
                "Trades Today",
                f"{summary['today_trades']} / {settings.get('max_trades_per_day', 10)}",
                border=True,
            )
            st.metric(
                "Realized P&L",
                f"${summary['realized_pnl']:+.2f}",
                delta=f"{summary['wins']}W / {summary['losses']}L",
                border=True,
            )
            st.metric(
                "Win Rate",
                f"{summary['win_rate']:.1f}%",
                f"{summary['closed_trades']} settled",
                border=True,
            )

        st.subheader("Paper Maintenance")
        with st.container(border=True):
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

