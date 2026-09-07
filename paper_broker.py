"""Simulated portfolio and state manager with daily trade limits,
signals cache, and activity logging for the Streamlit dashboard."""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import config
import database
import polymarket_client
import settings_manager


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PaperBroker:
    def __init__(self, state_path=None):
        self.state_path = state_path or config.STATE_FILE
        self.state = self._load()
        database.sync_from_state(self.state)

    def _load(self) -> Dict[str, Any]:
        default_state: Dict[str, Any] = {
            "balance": config.STARTING_BALANCE,
            "positions": {},   # token_id -> position dict
            "closed_trades": [],
            "signals": [],
            "daily_trades": {"date": _today_str(), "count": 0},
            "order_lifecycle": {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0},
            "logs": [],
        }
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        default_state.update(loaded)
            except Exception as e:
                print(f"[broker] Warning: Failed to read {self.state_path}: {e}")
        return default_state

    def save(self):
        try:
            tmp = f"{self.state_path}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:
            print(f"[broker] Error saving state: {e}")

    def add_log(self, message: str, level: str = "INFO"):
        entry = {
            "timestamp": _now_iso(),
            "level": level,
            "message": message,
        }
        logs = self.state.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 100:
            self.state["logs"] = logs[-100:]
        self.save()

    def save_signals(self, opportunities):
        self.state["signals"] = [o.to_dict() if hasattr(o, "to_dict") else dict(o) for o in opportunities]
        self.save()

    @property
    def open_exposure(self) -> float:
        return sum(p["stake"] for p in self.state["positions"].values())

    @property
    def held_token_ids(self) -> set:
        return {str(p.get("token_id", k)) for k, p in self.state.get("positions", {}).items()}

    @property
    def today_trades_count(self) -> int:
        dt = self.state.get("daily_trades", {})
        if dt.get("date") != _today_str():
            self.state["daily_trades"] = {"date": _today_str(), "count": 0}
            self.save()
            return 0
        return dt.get("count", 0)

    def can_open(self, stake: float) -> Tuple[bool, str]:
        settings = settings_manager.load_settings()
        max_positions = settings.get("max_open_positions", 10)
        max_exposure = settings.get("max_total_exposure", 200.0)
        max_daily_trades = settings.get("max_trades_per_day", 10)

        if self.today_trades_count >= max_daily_trades:
            return False, f"max daily trades limit reached ({self.today_trades_count}/{max_daily_trades})"

        if len(self.state["positions"]) >= max_positions:
            return False, f"max open positions reached ({len(self.state['positions'])}/{max_positions})"

        if self.open_exposure + stake > max_exposure:
            return False, f"would exceed max exposure (${self.open_exposure + stake:.2f} > ${max_exposure:.2f})"

        if stake > self.state["balance"]:
            return False, f"insufficient balance (${self.state['balance']:.2f} < ${stake:.2f})"

        return True, ""

    def record_intention(self):
        lc = self.state.setdefault("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0})
        lc["intentions"] = lc.get("intentions", 0) + 1
        self.save()

    def record_rejection(self):
        lc = self.state.setdefault("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0})
        lc["rejected"] = lc.get("rejected", 0) + 1
        self.save()

    def open_position(
        self,
        opp,
        stake: Optional[float] = None,
        mode: str = "PAPER",
        account_name: str = "Primary",
        wallet_address: Optional[str] = None,
        **kwargs
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        settings = settings_manager.load_settings()
        stake = stake if stake is not None else settings.get("stake_per_trade", 25.0)

        self.record_intention()
        ok, reason = self.can_open(stake)
        if not ok:
            self.record_rejection()
            return None, reason

        shares = stake / opp.confirmed_price
        clean_acc = "".join(c for c in account_name if c.isalnum() or c in ("_", "-"))
        trade_id = f"trd_ord_{clean_acc}_{str(opp.token_id)[:8]}"
        pos_key = f"{clean_acc}_{opp.token_id}" if account_name != "Primary" else str(opp.token_id)
        if pos_key in self.state.get("positions", {}):
            pos_key = f"{clean_acc}_{opp.token_id}_{int(time.time())}"

        time_left_str = "0.0m"
        if getattr(opp, "end_date", None):
            try:
                end_dt = datetime.fromisoformat(str(opp.end_date).replace("Z", "+00:00"))
                delta = end_dt - datetime.now(timezone.utc)
                total_min = int(delta.total_seconds() / 60)
                if total_min < 60:
                    time_left_str = f"{max(0, total_min)}m"
                elif total_min < 1440:
                    time_left_str = f"{total_min / 60:.1f}h"
                else:
                    time_left_str = f"{total_min / 1440:.1f}d"
            except Exception:
                time_left_str = "0.0m"

        slug_val = getattr(opp, "slug", "") or database.resolve_market_slug(opp.market_id)
        position = {
            "trade_id": trade_id,
            "mode": mode.upper(),
            "account_name": account_name,
            "wallet_address": wallet_address or "",
            "token_id": opp.token_id,
            "market_id": opp.market_id,
            "slug": slug_val,
            "event_id": getattr(opp, "event_id", "") or str(opp.market_id)[:12],
            "question": opp.question,
            "outcome_label": opp.outcome_label,
            "market_type": getattr(opp, "market_type", "moneyline"),
            "entry_price": opp.confirmed_price,
            "shares": shares,
            "stake": stake,
            "potential_payout": shares * 1.0,
            "potential_profit": (shares * 1.0) - stake,
            "time_left": time_left_str,
            "opened_at": _now_iso(),
            "end_date": opp.end_date,
            "game_start_time": getattr(opp, "game_start_time", None),
        }
        self.state["positions"][pos_key] = position
        self.state["balance"] -= stake

        # Record into local SQLite database
        database.record_trade({
            "trade_id": trade_id,
            "placed_at": position["opened_at"],
            "market_id": position["market_id"],
            "token_id": position["token_id"],
            "slug": slug_val,
            "question": position["question"],
            "outcome": position["outcome_label"],
            "entry_price": position["entry_price"],
            "tokens": position["shares"],
            "cost": position["stake"],
            "time_left": time_left_str,
            "result": "PENDING",
            "resolved_price": None,
            "payout": None,
            "pnl": 0.0,
            "broker": mode.lower(),
            "tx_hash": None,
            "closed_at": None,
            "note": "",
            "account_name": account_name,
            "wallet_address": wallet_address or "",
        })

        # Update lifecycle counter
        lc = self.state.setdefault("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0})
        lc["filled"] = lc.get("filled", 0) + 1

        # Update daily trades counter
        dt = self.state.setdefault("daily_trades", {"date": _today_str(), "count": 0})
        if dt.get("date") != _today_str():
            dt["date"] = _today_str()
            dt["count"] = 1
        else:
            dt["count"] += 1

        self.add_log(f"OPENED [{mode.upper()}][{account_name}]: {opp.question[:45]} [{opp.outcome_label}] @ {opp.confirmed_price:.3f} stake=${stake:.2f}")
        self.save()
        return position, ""

    def _close_position(self, pos_key: str, resolved_price: float, note: str) -> Dict[str, Any]:
        position = self.state["positions"].pop(pos_key)
        payout = position["shares"] * resolved_price
        pnl = payout - position["stake"]
        trade = {
            **position,
            "closed_at": _now_iso(),
            "resolved_price": resolved_price,
            "payout": payout,
            "pnl": pnl,
            "note": note,
        }
        self.state["closed_trades"].append(trade)
        self.state["balance"] += payout
        acc_str = f"[{position.get('account_name', 'Primary')}] " if position.get('account_name') else ""
        self.add_log(f"SETTLED {acc_str}: {position['question'][:45]} [{position['outcome_label']}] pnl={pnl:+.2f} ({note})")

        # Settle in local SQLite database
        database.settle_trade(
            token_id=str(position.get("token_id", pos_key)),
            trade_id=position.get("trade_id"),
            resolved_price=resolved_price,
            payout=payout,
            pnl=pnl,
            result="WON" if pnl > 0 else "LOST",
            closed_at=trade["closed_at"],
            note=note,
        )

        return trade

    def force_settle_position(self, token_id_or_key: str, won: bool = True, note: str = "") -> Optional[Dict[str, Any]]:
        """Manually settles an open position as WON (1.0) or LOST (0.0), booking P&L and updating SQLite DB."""
        positions = self.state.get("positions", {})
        target_key = None
        if token_id_or_key in positions:
            target_key = token_id_or_key
        else:
            for k, p in positions.items():
                if str(p.get("token_id")) == str(token_id_or_key) or str(p.get("trade_id")) == str(token_id_or_key):
                    target_key = k
                    break
        if not target_key:
            return None
        settle_price = 1.0 if won else 0.0
        settle_note = note or ("manual settlement: WON" if won else "manual settlement: LOST")
        trade = self._close_position(target_key, settle_price, settle_note)
        self.save()
        return trade

    def check_resolutions(self) -> List[Dict[str, Any]]:
        """Looks up each open position's market via the unified SDK or end_date elapsed checks;
        if closed or resolved, settles the position at the resolved price and books P&L."""
        if not self.state.get("positions"):
            return []

        settled = []
        client = polymarket_client.get_public_client()
        now_utc = datetime.now(timezone.utc)

        for pos_key, position in list(self.state["positions"].items()):
            market_id = position.get("market_id")
            token_id = str(position.get("token_id", pos_key))
            m = None
            try:
                m = client.get_market(id=str(market_id))
            except Exception as e:
                print(f"[paper_broker] Market {market_id} lookup notice: {e}")

            # 1. Check official API resolution state
            if m and m.state:
                uma_status = str(m.resolution.uma_resolution_status).lower() if (m.resolution and m.resolution.uma_resolution_status) else ""
                is_uma_resolved = "resolved" in uma_status

                raw_price = None
                if m.outcomes:
                    for outcome in [m.outcomes.yes, m.outcomes.no]:
                        if outcome and outcome.token_id and str(outcome.token_id) == token_id:
                            if outcome.price is not None:
                                raw_price = float(outcome.price)
                            break

                if is_uma_resolved or (m.state.closed and raw_price is not None and (raw_price >= 0.95 or raw_price <= 0.05)):
                    final_settle_price = 1.0 if (raw_price is not None and raw_price >= 0.5) or is_uma_resolved else 0.0
                    note = "settled (win)" if final_settle_price == 1.0 else "settled (loss)"
                    trade = self._close_position(pos_key, final_settle_price, note)
                    settled.append(trade)
                    continue

            # 2. Check match elapsed time
            # If match end_date has passed by more than 2 hours and market is closed/inactive
            end_date_str = position.get("end_date")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
                    if (now_utc - end_dt) > timedelta(hours=2):
                        is_ended = False
                        if m and m.state and (m.state.closed or not m.state.accepting_orders):
                            is_ended = True
                        elif (now_utc - end_dt) > timedelta(hours=10):
                            # Completed match whose resolution window has passed
                            is_ended = True

                        if is_ended:
                            final_settle_price = 1.0
                            note = f"settled (match completed at {str(end_dt)[:16]})"
                            trade = self._close_position(pos_key, final_settle_price, note)
                            settled.append(trade)
                except Exception as exc:
                    print(f"[paper_broker] Error checking end_date for {token_id}: {exc}")

        if settled:
            self.save()
        return settled

    def summary(self) -> Dict[str, Any]:
        closed = self.state.get("closed_trades", [])
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
        return {
            "balance": self.state.get("balance", config.STARTING_BALANCE),
            "open_positions": len(self.state.get("positions", {})),
            "open_exposure": self.open_exposure,
            "today_trades": self.today_trades_count,
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "realized_pnl": sum(t.get("pnl", 0) for t in closed),
            "reserved_capital": self.open_exposure,
            "order_lifecycle": self.state.get("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0}),
        }

