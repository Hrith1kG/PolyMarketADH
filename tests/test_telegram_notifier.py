import pytest
from unittest.mock import patch, MagicMock
import telegram_notifier


def test_is_configured():
    with patch("telegram_notifier.get_credentials", return_value=("fake_token", "fake_chat_id")):
        assert telegram_notifier.is_configured() is True

    with patch("telegram_notifier.get_credentials", return_value=("", "fake_chat_id")):
        assert telegram_notifier.is_configured() is False

    with patch("telegram_notifier.get_credentials", return_value=("fake_token", "")):
        assert telegram_notifier.is_configured() is False


def test_format_duration():
    d = telegram_notifier.format_duration("2026-09-13 12:00:00", "2026-09-13 12:45:30")
    assert "45m" in d

    d_hours = telegram_notifier.format_duration("2026-09-13 10:00:00", "2026-09-13 12:30:00")
    assert "2h 30m" in d_hours

    assert telegram_notifier.format_duration(None, None) == "N/A"


@patch("telegram_notifier.requests.post")
def test_send_message_success(mock_post):
    mock_post.return_value.json.return_value = {"ok": True}
    with patch("telegram_notifier.get_credentials", return_value=("token123", "chat456")):
        result = telegram_notifier.send_message("<b>Hello World</b>")
        assert result is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"]["chat_id"] == "chat456"
        assert "Hello World" in kwargs["json"]["text"]


@patch("telegram_notifier.requests.post")
def test_send_message_failure_handles_gracefully(mock_post):
    mock_post.side_effect = Exception("Network timeout")
    with patch("telegram_notifier.get_credentials", return_value=("token123", "chat456")):
        result = telegram_notifier.send_message("Test message")
        assert result is False


@patch("telegram_notifier.requests.post")
def test_send_message_multiple_chats(mock_post):
    mock_post.return_value.json.return_value = {"ok": True}
    with patch("telegram_notifier.get_credentials", return_value=("token123", "431736948, -1003964142758")):
        result = telegram_notifier.send_message("Multi broadcast")
        assert result is True
        assert mock_post.call_count == 2
        calls = [call[1]["json"]["chat_id"] for call in mock_post.call_args_list]
        assert "431736948" in calls
        assert "-1003964142758" in calls



@patch("telegram_notifier.send_message")
def test_notify_trade_entry(mock_send):
    with patch("telegram_notifier.is_configured", return_value=True):
        trade = {
            "trade_id": "trd_12345",
            "question": "Will BTC reach 100k?",
            "outcome": "YES",
            "entry_price": 0.75,
            "tokens": 100.0,
            "cost": 75.0,
            "account_name": "TestAccount",
            "mode": "LIVE",
            "time_left": "15m",
        }
        telegram_notifier.notify_trade_entry(trade, async_send=False)
        assert mock_send.called
        msg = mock_send.call_args[0][0]
        assert "TRADE ENTERED" in msg
        assert "[LIVE]" in msg
        assert "Will BTC reach 100k?" in msg
        assert "0.7500" in msg
        assert "TestAccount" in msg


@patch("telegram_notifier.send_message")
def test_notify_trade_exit_profit(mock_send):
    with patch("telegram_notifier.is_configured", return_value=True):
        trade = {
            "trade_id": "trd_12345",
            "question": "Will BTC reach 100k?",
            "outcome": "YES",
            "entry_price": 0.75,
            "resolved_price": 1.0,
            "tokens": 100.0,
            "cost": 75.0,
            "payout": 100.0,
            "pnl": 25.0,
            "result": "WON",
            "broker": "live",
            "account_name": "TestAccount",
            "placed_at": "2026-09-13 12:00:00",
            "closed_at": "2026-09-13 12:30:00",
            "note": "Market resolved WON",
        }
        telegram_notifier.notify_trade_exit(trade, async_send=False)
        assert mock_send.called
        msg = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in msg
        assert "[PROFIT]" in msg
        assert "+$25.00" in msg
        assert "+33.33%" in msg
        assert "Market resolved WON" in msg


@patch("telegram_notifier.send_message")
def test_notify_trade_exit_loss(mock_send):
    with patch("telegram_notifier.is_configured", return_value=True):
        trade = {
            "trade_id": "trd_loss_1",
            "question": "Will ETH reach 5k?",
            "outcome": "YES",
            "entry_price": 0.95,
            "resolved_price": 0.0,
            "tokens": 100.0,
            "cost": 95.0,
            "payout": 0.0,
            "pnl": -95.0,
            "result": "LOST",
            "broker": "live",
            "account_name": "TestAccount",
            "placed_at": "2026-09-13 12:00:00",
            "closed_at": "2026-09-13 12:15:00",
            "note": "Stop Loss triggered",
        }
        telegram_notifier.notify_trade_exit(trade, async_send=False)
        assert mock_send.called
        msg = mock_send.call_args[0][0]
        assert "POSITION CLOSED" in msg
        assert "[LOSS]" in msg
        assert "-$95.00" in msg


@patch("telegram_notifier.send_message")
def test_notify_void_trade_skipped(mock_send):
    with patch("telegram_notifier.is_configured", return_value=True):
        trade = {
            "trade_id": "trd_void",
            "question": "Some market",
            "result": "VOID",
        }
        telegram_notifier.notify_trade_exit(trade, async_send=False)
        assert not mock_send.called
