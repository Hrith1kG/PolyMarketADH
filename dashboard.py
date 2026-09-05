"""Streamlit Primary Control Panel for Polymarket Sureshot Trading Bot.
Supports seamless switching between Paper and Live Execution, Live Account Vitals,
and full strategy lifecycle monitoring."""
import json
import os
import time
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

import config
from paper_broker import PaperBroker
import live_broker
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
    st.subheader("Threshold & Strategy Settings")
    with st.form("settings_form"):
        st.markdown("**Probability & Odds Limits**")
        price_min = st.slider(
            "Min Price (Entry Floor)",
            min_value=0.85,
            max_value=0.995,
            value=float(settings.get("price_min", 0.97)),
            step=0.005,
            format="%.3f",
            help="Minimum implied probability (e.g. 0.97 = 97%)",
        )
        price_max = st.slider(
            "Max Price (Entry Ceiling)",
            min_value=0.95,
            max_value=0.999,
            value=float(settings.get("price_max", 0.995)),
            step=0.001,
            format="%.3f",
            help="Maximum implied probability. Beyond this, upside is too thin.",
        )

        st.markdown("**Volume & Liquidity Floors**")
        min_volume = st.number_input(
            "Min 24h Volume ($)",
            min_value=0.0,
            max_value=100000.0,
            value=float(settings.get("min_volume", 5000.0)),
            step=500.0,
            help="Filters out inactive or abandoned markets",
        )
        min_liquidity = st.number_input(
            "Min Book Liquidity ($)",
            min_value=0.0,
            max_value=50000.0,
            value=float(settings.get("min_liquidity", 1000.0)),
            step=250.0,
            help="Filters out thin order books vulnerable to manipulation",
        )

        st.markdown("**Resolution Timing**")
        col1, col2 = st.columns(2)
        with col1:
            min_hours = st.number_input(
                "Min Hours",
                min_value=0.1,
                max_value=48.0,
                value=float(settings.get("min_hours_to_resolution", 1.0)),
                step=0.5,
                help="Avoids last-minute chaos before game ends",
            )
        with col2:
            max_days = st.number_input(
                "Max Days",
                min_value=1.0,
                max_value=90.0,
                value=float(settings.get("max_days_to_resolution", 30.0)),
                step=1.0,
                help="Prevents capital getting tied up for too long",
            )

        st.markdown("**Risk & Position Sizing**")
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

        st.markdown("**Daily Limits & Throttles**")
        max_daily_trades = st.number_input(
            "Max Trades Per Day",
            min_value=1,
            max_value=100,
            value=int(settings.get("max_trades_per_day", 10)),
            step=1,
            help="Ceiling on number of positions opened in a single day",
        )
        max_signals = st.number_input(
            "Max Signals Per Scan",
            min_value=1,
            max_value=25,
            value=int(settings.get("max_signals_per_scan", 5)),
            step=1,
            help="Number of top candidates retained per scan cycle",
        )

        st.markdown("**Category & Focus**")
        only_sports = st.checkbox(
            "Focus Exclusively on Sports",
            value=bool(settings.get("only_sports", True)),
            help="Limits scanning to Polymarket Sports master tag (100639)",
        )
        only_moneyline = st.checkbox(
            "Moneyline Matches Only",
            value="moneyline" in settings.get("sports_market_types", ["moneyline"]),
            help="Focus exclusively on match winners (excludes props/spreads)",
        )
        poll_interval = st.number_input(
            "Scan Interval (Seconds)",
            min_value=10,
            max_value=600,
            value=int(settings.get("poll_interval_seconds", 60)),
            step=10,
        )

        saved = st.form_submit_button("💾 Save Settings")
        if saved:
            updated_settings = {
                "price_min": price_min,
                "price_max": price_max,
                "min_volume": min_volume,
                "min_liquidity": min_liquidity,
                "min_hours_to_resolution": min_hours,
                "max_days_to_resolution": max_days,
                "stake_per_trade": stake_per_trade,
                "max_open_positions": max_positions,
                "max_total_exposure": max_exposure,
                "max_trades_per_day": max_daily_trades,
                "max_signals_per_scan": max_signals,
                "only_sports": only_sports,
                "sports_market_types": ["moneyline"] if only_moneyline else [],
                "poll_interval_seconds": poll_interval,
            }
            settings.update(updated_settings)
            settings_manager.save_settings(settings)
            st.success("Settings updated successfully! The bot will apply them on the next cycle.")
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
                df_pos.append({
                    "Mode": mode,
                    "Event ID": event_id,
                    "Token": token_short,
                    "Side": "BUY",
                    "Quantity": f"{p.get('shares', 0):.2f}",
                    "Avg Entry": f"${p.get('entry_price', 0):.4f}",
                    "Capital": f"${p.get('stake', 0):.2f}",
                    "Question": p.get("question", "")[:45],
                })
            st.dataframe(pd.DataFrame(df_pos))


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
            df_signals.append({
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
        st.dataframe(pd.DataFrame(df_signals))

        # Manual Trade Executor
        st.markdown("### Manual Trade Trigger")
        st.write(f"Execute a trade in **{execution_mode_str}** mode on one of the signals:")
        signal_options = {f"{s['question'][:60]} ({s['outcome_label']} @ {s['confirmed_price']:.3f})": s for s in signals}
        selected_signal_name = st.selectbox("Select Signal to Trade", list(signal_options.keys()))

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

                pos, reason = broker.open_position(obj, stake=manual_stake, mode=execution_mode_str)
                if pos:
                    st.success(f"Successfully opened position on {obj.question[:40]} with ${manual_stake} stake!")
                    st.rerun()
                else:
                    st.error(f"Cannot open position: {reason}")


# =============================================================
# TAB 5: TRADE HISTORY
# =============================================================
with tab_history:
    st.header("📜 Resolved & Settled Trades Log")
    closed = state.get("closed_trades", [])

    if not closed:
        st.info("No closed trades recorded yet.")
    else:
        df_closed = []
        for t in reversed(closed):
            pnl = t.get("pnl", 0)
            status_tag = "WIN 🟢" if pnl > 0 else "LOSS 🔴"
            df_closed.append({
                "Status": status_tag,
                "Mode": t.get("mode", "PAPER"),
                "Match": t.get("question"),
                "Outcome": t.get("outcome_label"),
                "Stake": f"${t.get('stake', 0):.2f}",
                "Payout": f"${t.get('payout', 0):.2f}",
                "P&L ($)": f"{pnl:+.2f}",
                "Resolved Price": f"{t.get('resolved_price', 0):.2f}",
                "Closed At": t.get("closed_at", "")[:19].replace("T", " "),
                "Note": t.get("note", ""),
            })
        st.dataframe(pd.DataFrame(df_closed))


# =============================================================
# TAB 6: PERFORMANCE ANALYTICS
# =============================================================
with tab_perf:
    st.header("📊 Performance Analytics")

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
    st.subheader("Danger Zone")
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("🗑️ Reset State / Paper Portfolio", help="Resets balance to starting capital and clears positions"):
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
            st.success("Portfolio reset successfully!")
            st.rerun()
    with col_r2:
        if st.button("🔄 Reset Settings to Defaults"):
            settings_manager.save_settings(settings_manager.DEFAULT_SETTINGS)
            st.success("Settings restored to factory defaults!")
            st.rerun()

