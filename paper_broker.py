"""Simulated portfolio: no real orders, no wallet. Tracks hypothetical fills so you
can validate the strategy's hit-rate and P&L before ever risking real funds."""
import json
import os
from datetime import datetime, timezone

import requests

import config
import gamma_client


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class PaperBroker:
    def __init__(self, state_path=None):
        self.state_path = state_path or config.STATE_FILE
        self.state = self._load()

    def _load(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, "r") as f:
                return json.load(f)
        return {
            "balance": config.STARTING_BALANCE,
            "positions": {},   # token_id -> position dict
            "closed_trades": [],
        }

    def save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    @property
    def open_exposure(self):
        return sum(p["stake"] for p in self.state["positions"].values())

    @property
    def held_token_ids(self):
        return set(self.state["positions"].keys())

    def can_open(self, stake):
        if len(self.state["positions"]) >= config.MAX_OPEN_POSITIONS:
            return False, "max open positions reached"
        if self.open_exposure + stake > config.MAX_TOTAL_EXPOSURE:
            return False, "would exceed max total exposure"
        if stake > self.state["balance"]:
            return False, "insufficient paper balance"
        return True, ""

    def open_position(self, opp, stake=None):
        stake = stake if stake is not None else config.STAKE_PER_TRADE
        ok, reason = self.can_open(stake)
        if not ok:
            return None, reason

        shares = stake / opp.confirmed_price
        position = {
            "token_id": opp.token_id,
            "market_id": opp.market_id,
            "question": opp.question,
            "outcome_label": opp.outcome_label,
            "entry_price": opp.confirmed_price,
            "shares": shares,
            "stake": stake,
            "opened_at": _now_iso(),
            "end_date": opp.end_date,
        }
        self.state["positions"][opp.token_id] = position
        self.state["balance"] -= stake
        self.save()
        return position, ""

    def _close_position(self, token_id, resolved_price, note):
        position = self.state["positions"].pop(token_id)
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
        return trade

    def check_resolutions(self):
        """Looks up each open position's market; if Gamma reports it closed/resolved,
        settles the paper position at the resolved price (0 or 1) and books P&L."""
        if not self.state["positions"]:
            return []

        settled = []
        market_ids = {p["market_id"] for p in self.state["positions"].values()}
        raw_markets = {}

        for market_id in market_ids:
            try:
                resp = requests.get(f"{config.GAMMA_BASE_URL}/markets/{market_id}", timeout=15)
                resp.raise_for_status()
                raw_markets[market_id] = resp.json()
            except Exception:
                continue

        for token_id, position in list(self.state["positions"].items()):
            raw = raw_markets.get(position["market_id"])
            if not raw or not raw.get("closed"):
                continue
            for record in gamma_client.parse_market(raw):
                if record["token_id"] == token_id:
                    resolved_price = record["price"]  # settles to ~0 or ~1 once closed
                    trade = self._close_position(token_id, resolved_price, "market closed")
                    settled.append(trade)
                    break
        if settled:
            self.save()
        return settled

    def summary(self):
        closed = self.state["closed_trades"]
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        return {
            "balance": self.state["balance"],
            "open_positions": len(self.state["positions"]),
            "open_exposure": self.open_exposure,
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "realized_pnl": sum(t["pnl"] for t in closed),
        }
