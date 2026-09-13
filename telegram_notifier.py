"""Telegram notification module for Polymarket trading bot.
Dispatches real-time alerts for trade entries, exits, and PnL reporting.
Runs asynchronously in daemon threads to guarantee that network latency or Telegram outages
never delay or disrupt bot execution.
"""
import html
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

# Ensure environment is loaded
load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
REQUEST_TIMEOUT = 6.0


def get_credentials() -> Tuple[str, str]:
    """Retrieves current Telegram Bot Token and Chat ID from environment or settings."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def is_configured() -> bool:
    """Returns True if both bot token and chat ID are present."""
    token, chat_id = get_credentials()
    return bool(token and chat_id)


def test_connection() -> Tuple[bool, str]:
    """Validates the bot token via Telegram getMe API.
    Returns (success, bot_username_or_error_message).
    """
    token, chat_id = get_credentials()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is not configured."
    if not chat_id:
        return False, "TELEGRAM_CHAT_ID is not configured."

    url = f"{TELEGRAM_API_URL}/bot{token}/getMe"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if data.get("ok"):
            username = data.get("result", {}).get("username", "UnknownBot")
            return True, f"@{username}"
        return False, f"Telegram API Error: {data.get('description', 'Unknown error')}"
    except Exception as exc:
        return False, f"Connection error: {exc}"


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Sends a message synchronously to the configured Telegram chat.
    Catches all exceptions to prevent breaking the caller.
    """
    token, chat_id = get_credentials()
    if not token or not chat_id:
        logger.debug("[Telegram] Token or Chat ID not configured. Skipping alert.")
        return False

    url = f"{TELEGRAM_API_URL}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"[Telegram] Failed to send message: {data.get('description')}")
            return False
        return True
    except Exception as exc:
        logger.warning(f"[Telegram] Error sending message: {exc}")
        return False


def send_message_async(text: str, parse_mode: str = "HTML") -> None:
    """Sends a message in a background daemon thread so it never blocks execution."""
    thread = threading.Thread(target=send_message, args=(text, parse_mode), daemon=True)
    thread.start()


def format_duration(start_iso: Optional[str], end_iso: Optional[str]) -> str:
    """Computes a human-readable duration between two timestamps."""
    if not start_iso or not end_iso:
        return "N/A"
    try:
        def _parse(ts_str: str) -> datetime:
            clean = ts_str.replace("Z", "+00:00")
            if " " in clean and "T" not in clean:
                clean = clean.replace(" ", "T")
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        t1 = _parse(start_iso)
        t2 = _parse(end_iso)
        diff = abs((t2 - t1).total_seconds())

        hours = int(diff // 3600)
        minutes = int((diff % 3600) // 60)
        seconds = int(diff % 60)

        if hours > 24:
            days = hours // 24
            rem_hours = hours % 24
            return f"{days}d {rem_hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    except Exception:
        return "N/A"


def notify_trade_entry(trade: Dict[str, Any], async_send: bool = True) -> None:
    """Formats and dispatches an entry alert when a position is opened."""
    if not is_configured():
        return

    try:
        import settings_manager
        settings = settings_manager.get_settings()
        if not settings.get("telegram_notifications_enabled", True):
            return
        if not settings.get("telegram_notify_entries", True):
            return
        mode = str(trade.get("mode") or trade.get("broker") or "PAPER").upper()
        if mode == "PAPER" and not settings.get("telegram_notify_paper", True):
            return
    except Exception:
        pass

    mode = str(trade.get("mode") or trade.get("broker") or "PAPER").upper()
    mode_badge = f"🟢 [{mode}]" if mode == "LIVE" else f"📝 [{mode}]"

    question = html.escape(str(trade.get("question") or "Unknown Market"))
    outcome = html.escape(str(trade.get("outcome") or trade.get("outcome_label") or "YES"))
    price = float(trade.get("entry_price") or trade.get("price") or 0.0)
    tokens = float(trade.get("tokens") or trade.get("shares") or 0.0)
    cost = float(trade.get("cost") or trade.get("stake") or 0.0)
    account = html.escape(str(trade.get("account_name") or "Primary"))
    time_left = html.escape(str(trade.get("time_left") or ""))
    trade_id = html.escape(str(trade.get("trade_id") or "")[:18])

    msg = (
        f"<b>{mode_badge} TRADE ENTERED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Market:</b> {question}\n"
        f"<b>Outcome:</b> <code>{outcome}</code> @ <b>${price:.4f}</b>\n"
        f"<b>Size:</b> {tokens:,.2f} shares (Cost: <b>${cost:.2f}</b>)\n"
        f"<b>Account:</b> {account}\n"
    )
    if time_left and time_left != "0.0m":
        msg += f"<b>Time Left:</b> {time_left}\n"
    if trade_id:
        msg += f"<b>Trade ID:</b> <code>{trade_id}</code>\n"

    msg += f"<b>Timestamp:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"

    if async_send:
        send_message_async(msg)
    else:
        send_message(msg)


def notify_trade_exit(trade: Dict[str, Any], async_send: bool = True) -> None:
    """Formats and dispatches an exit alert when a position is closed/settled."""
    if not is_configured():
        return

    try:
        import settings_manager
        settings = settings_manager.get_settings()
        if not settings.get("telegram_notifications_enabled", True):
            return
        if not settings.get("telegram_notify_exits", True):
            return
        mode = str(trade.get("broker") or trade.get("mode") or "PAPER").upper()
        if mode == "PAPER" and not settings.get("telegram_notify_paper", True):
            return
    except Exception:
        pass

    mode = str(trade.get("broker") or trade.get("mode") or "PAPER").upper()
    pnl = float(trade.get("pnl") or 0.0)
    cost = float(trade.get("cost") or 0.0)
    payout = float(trade.get("payout") or 0.0)
    entry_price = float(trade.get("entry_price") or 0.0)
    exit_price = float(trade.get("resolved_price") if trade.get("resolved_price") is not None else 0.0)
    result = str(trade.get("result") or "CLOSED").upper()

    # Don't notify VOID orders (they never filled)
    if result == "VOID":
        return

    roi_pct = (pnl / cost * 100.0) if cost > 0 else 0.0

    if pnl > 0.001:
        badge = "🎯 [PROFIT]"
        pnl_str = f"+${pnl:.2f} (+{roi_pct:.2f}%) 🚀"
    elif pnl < -0.001:
        badge = "🔻 [LOSS]"
        pnl_str = f"-${abs(pnl):.2f} ({roi_pct:.2f}%)"
    else:
        badge = "⚪ [EVEN]"
        pnl_str = f"$0.00 (0.00%)"

    question = html.escape(str(trade.get("question") or "Unknown Market"))
    outcome = html.escape(str(trade.get("outcome") or "YES"))
    account = html.escape(str(trade.get("account_name") or "Primary"))
    note = html.escape(str(trade.get("note") or ""))
    trade_id = html.escape(str(trade.get("trade_id") or "")[:18])

    placed_at = trade.get("placed_at")
    closed_at = trade.get("closed_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    duration = format_duration(placed_at, closed_at)

    msg = (
        f"<b>{badge} POSITION CLOSED [{mode}]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Market:</b> {question}\n"
        f"<b>Outcome:</b> <code>{outcome}</code> ({result})\n"
        f"<b>Entry ➔ Exit:</b> ${entry_price:.4f} ➔ <b>${exit_price:.4f}</b>\n"
        f"<b>Realized PnL:</b> <b>{pnl_str}</b>\n"
        f"<b>Payout / Cost:</b> ${payout:.2f} / ${cost:.2f}\n"
        f"<b>Hold Duration:</b> {duration}\n"
        f"<b>Account:</b> {account}\n"
    )
    if note:
        msg += f"<b>Reason:</b> {note}\n"
    if trade_id:
        msg += f"<b>Trade ID:</b> <code>{trade_id}</code>\n"

    if async_send:
        send_message_async(msg)
    else:
        send_message(msg)


def send_test_notification() -> Tuple[bool, str]:
    """Sends a sample test alert to confirm end-to-end delivery."""
    token, chat_id = get_credentials()
    if not token or not chat_id:
        return False, "Bot token or Chat ID is missing."

    test_msg = (
        f"🤖 <b>Polymarket Bot Connected!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Real-time trade & exit notifications are active.\n"
        f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"<i>You will receive alerts here whenever trades are opened or closed.</i>"
    )
    success = send_message(test_msg)
    if success:
        return True, "Test message sent successfully! Check your Telegram app."
    return False, "Failed to send message. Please verify your Bot Token and Chat ID."
