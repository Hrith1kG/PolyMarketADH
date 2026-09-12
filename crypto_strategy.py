"""Execution loop for the Crypto 5-Minute strategy.

Runs entirely independently of the sports loop in main.py: its own settings,
its own candidate discovery (crypto_scanner), its own polling cadence, its own
risk budget and its own kill switch. main.py runs it on a separate thread, so
speeding crypto up to a 2-5 second cadence does not change how often the sports
scan runs, and pausing one does not pause the other.

The entry sequence is deliberately claim-first:

    1. scan            -> sides that currently clear every gate
    2. claim the round -> atomic, persisted; exactly one caller wins
    3. re-check        -> market status, seconds remaining, executable ask,
                          slippage and every risk cap, re-read fresh
    4. submit          -> only then does an order reach the exchange
    5. confirm/release -> a claim behind a submitted order is kept forever;
                          a claim that never produced an order is given back

Claiming before the re-check is what makes "one entry per market/round" hold
across overlapping polls, two processes and a restart. Re-checking after the
claim is what stops the bot acting on a quote that has already moved: between
a scan and an order, a five-minute round can change completely.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import crypto_markets
import crypto_scanner
import polymarket_client
import settings_manager
from crypto_markets import (
    MarketRejected,
    REASON_EXPIRED,
    REASON_NOT_LIVE,
    REASON_NO_BOOK,
    REASON_PRICE_ABOVE_CEILING,
    REASON_PRICE_BELOW_THRESHOLD,
    REASON_RISK_BLOCKED,
    REASON_SLIPPAGE,
)

# How long the same (round, reason) skip message is suppressed for. At a
# 3-second cadence an unqualified round would otherwise emit the identical line
# ten times per window and bury everything else.
SKIP_LOG_THROTTLE_SECONDS = 30.0


@dataclass
class CryptoPassResult:
    """What one scan/trade pass did, for logging and for the tests."""

    scanned: int = 0
    signals: int = 0
    entries: int = 0
    skipped: int = 0
    rejected: int = 0
    error: Optional[str] = None
    entered_round_keys: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


class CryptoStrategy:
    """The crypto side of the bot. Holds no sports state and reads no sports setting."""

    def __init__(
        self,
        broker,
        client: Any = None,
        live_provider: Optional[Callable[[], Any]] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.broker = broker
        self._client = client
        self._live_provider = live_provider
        self._log = log or (lambda msg: print(msg))
        self._skip_log_seen: Dict[str, float] = {}
        self._stop = threading.Event()

    # --- plumbing ---------------------------------------------------------

    @property
    def client(self):
        return self._client or polymarket_client.get_public_client()

    def _live_broker(self, settings: Dict[str, Any]):
        """The live broker to trade through, or None for paper execution.

        Crypto follows the same global live/paper switch as sports -- it is a
        property of the wallet, not of the strategy -- but nothing else.
        """
        if not settings.get("live_trading", False):
            return None
        if self._live_provider is not None:
            return self._live_provider()
        try:
            import live_broker
            return live_broker.get_live_broker()
        except Exception as exc:
            self.log(f"[crypto] live broker unavailable, staying in PAPER: {exc}")
            return None

    def log(self, message: str) -> None:
        self._log(message)

    def log_skip(self, message: str, throttle_key: Optional[str] = None) -> None:
        """Logs a skip, suppressing an identical repeat within the throttle window."""
        key = throttle_key or message
        now = time.monotonic()
        last = self._skip_log_seen.get(key)
        if last is not None and (now - last) < SKIP_LOG_THROTTLE_SECONDS:
            return
        self._skip_log_seen[key] = now
        if len(self._skip_log_seen) > 512:
            cutoff = now - SKIP_LOG_THROTTLE_SECONDS
            self._skip_log_seen = {k: v for k, v in self._skip_log_seen.items() if v >= cutoff}
        self.log(message)

    def _broker_log(self, message: str, level: str = "INFO") -> None:
        try:
            self.broker.add_log(message, level=level)
        except Exception:  # pragma: no cover - logging must never break trading
            pass

    # --- risk -------------------------------------------------------------

    def _crypto_budget_ok(self, cfg: Dict[str, Any], stake: float) -> Tuple[bool, str]:
        """The crypto strategy's own budget, scoped to crypto positions only.

        Kept apart from the sports caps on purpose: a day of sports entries must
        not consume the crypto allowance, and vice versa. The per-account caps
        in PaperBroker.can_open still apply on top of this -- they guard the
        wallet, which both strategies genuinely share.
        """
        positions = self.broker.crypto_positions()
        if len(positions) >= cfg["max_open_positions"]:
            return False, (
                f"crypto max open positions reached ({len(positions)}/{cfg['max_open_positions']})"
            )
        exposure = sum(float(p.get("stake", 0.0)) for p in positions.values())
        if exposure + stake > cfg["max_total_exposure"]:
            return False, (
                f"would exceed crypto max exposure (${exposure + stake:.2f} > "
                f"${cfg['max_total_exposure']:.2f})"
            )
        today = self.broker.crypto_trades_today()
        if today >= cfg["max_trades_per_day"]:
            return False, f"crypto max trades per day reached ({today}/{cfg['max_trades_per_day']})"
        return True, ""

    # --- pre-trade re-check ----------------------------------------------

    def recheck(self, opp, cfg: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[bool, str, str, float]:
        """Re-validates everything immediately before the order is submitted.

        Re-reads the market and the order book from the API rather than trusting
        the scan: status, round length, seconds remaining, the executable ask and
        the slippage against the quote the signal was built on are all confirmed
        again. Returns (ok, reason, detail, executable_price).
        """
        now = now or crypto_markets.utc_now()
        try:
            market = self.client.get_market(id=str(opp.market_id))
        except Exception as exc:
            return False, REASON_NOT_LIVE, f"could not re-read market ({type(exc).__name__}: {exc})", 0.0

        live_ok, live_detail = crypto_markets.is_market_live(market)
        if not live_ok:
            return False, REASON_NOT_LIVE, live_detail, 0.0

        try:
            round_ = crypto_markets.classify_market(
                market,
                allowed_symbols=cfg["assets"],
                expected_duration_seconds=cfg["round_duration_seconds"],
                duration_tolerance_seconds=cfg["round_duration_tolerance_seconds"],
            )
        except MarketRejected as rejected:
            return False, rejected.reason, rejected.detail, 0.0

        if round_.round_key != opp.round_key:
            return False, REASON_EXPIRED, (
                f"round rolled over between scan and order ({opp.round_key} -> {round_.round_key})"
            ), 0.0
        if round_.side_tokens.get(opp.side) != str(opp.token_id):
            return False, REASON_NOT_LIVE, "outcome token no longer matches this side of the round", 0.0

        remaining = round_.seconds_remaining(now)
        window_ok, window_reason, window_detail = crypto_markets.entry_window_check(
            remaining, cfg["entry_window_seconds"]
        )
        if not window_ok:
            return False, window_reason, window_detail, 0.0

        try:
            order_book = self.client.get_order_book(token_id=str(opp.token_id))
        except Exception as exc:
            return False, REASON_NO_BOOK, f"order book unavailable ({type(exc).__name__}: {exc})", 0.0

        required_shares = cfg["stake_per_trade"] / max(float(opp.confirmed_price), 1e-9)
        ask, book_reason, book_detail, _info = crypto_scanner.evaluate_book(
            order_book,
            required_shares=required_shares,
            max_spread=cfg["max_spread"],
            max_quote_age_seconds=cfg["max_quote_age_seconds"],
            min_depth_multiple=cfg["min_ask_depth_multiple"],
            now=now,
        )
        if ask is None:
            return False, book_reason, book_detail, 0.0

        if ask < cfg["min_probability"]:
            return False, REASON_PRICE_BELOW_THRESHOLD, (
                f"executable ask {ask:.4f} < {cfg['min_probability']:.4f} floor at submit time"
            ), 0.0
        if ask > cfg["max_probability"]:
            return False, REASON_PRICE_ABOVE_CEILING, (
                f"executable ask {ask:.4f} > {cfg['max_probability']:.4f} ceiling at submit time"
            ), 0.0

        drift = ask - float(opp.confirmed_price)
        if cfg["max_slippage"] > 0 and drift > cfg["max_slippage"]:
            return False, REASON_SLIPPAGE, (
                f"price moved {drift:+.4f} (ask {ask:.4f} vs quote {float(opp.confirmed_price):.4f}), "
                f"over the {cfg['max_slippage']:.4f} cap"
            ), 0.0

        return True, "", f"{remaining:.1f}s left, executable ask {ask:.4f}", float(ask)

    # --- execution --------------------------------------------------------

    def _eligible_accounts(self, live) -> Dict[str, Dict[str, Any]]:
        """Enabled accounts that are not individually paused or kill-switched.

        Those two are safety controls on the account itself, so they bind every
        strategy. The account's price/volume/sports-type filters are not applied
        -- they describe the sports strategy and say nothing about a crypto round.
        """
        eligible: Dict[str, Dict[str, Any]] = {}
        for acc in config.get_configured_accounts():
            if not acc.get("enabled", True):
                continue
            name = acc["name"]
            if live is not None and live.get_session(name) is None:
                continue
            acc_settings = settings_manager.get_account_settings(name)
            if str(acc_settings.get("bot_status", "RUNNING")).upper() == "PAUSED":
                continue
            if acc_settings.get("entry_kill_switch", False):
                continue
            eligible[name] = acc_settings
        return eligible

    def _execute_live(self, live, opp, price: float, cfg: Dict[str, Any]) -> int:
        """Fires the entry for every eligible account and books the real fills.

        Returns the number of accounts that actually ended up holding shares.
        """
        eligible = self._eligible_accounts(live)
        stake = cfg["stake_per_trade"]
        stakes: Dict[str, float] = {}
        for name, acc_settings in eligible.items():
            held = {str(p.get("token_id")) for p in self.broker.positions_for_account(name).values()}
            if str(opp.token_id) in held:
                self.log_skip(
                    f"[crypto] SKIP [{name}] {opp.asset} {opp.side}: outcome already held by this account",
                    throttle_key=f"{opp.round_key}:dup:{name}",
                )
                continue
            limits = {
                "max_open_positions": acc_settings.get("max_open_positions"),
                "max_total_exposure": acc_settings.get("max_total_exposure"),
                "max_trades_per_day": acc_settings.get("max_trades_per_day"),
            }
            ok, reason = self.broker.can_open(stake, account_name=name, limits=limits)
            if not ok:
                self.broker.record_intention()
                self.broker.record_rejection()
                self.log_skip(
                    f"[crypto] SKIP [{name}] {opp.asset} {opp.side}: {reason}",
                    throttle_key=f"{opp.round_key}:risk:{name}",
                )
                continue
            stakes[name] = stake

        if not stakes:
            return 0

        results = live.place_buy_selected(
            str(opp.token_id), float(price), stakes, order_type=cfg["order_type"]
        )
        # "Entered" means an order reached the exchange -- a fill, or an order
        # left resting in the book. Both must keep the round's claim: a resting
        # order is real intent that a second poll would duplicate. This is
        # counted from the results BEFORE any bookkeeping, so a failure while
        # recording a fill can never report the round as un-entered and let a
        # later poll place a second order against it.
        entered_accounts = sum(1 for res in results if res["success"] or res["resting"])
        for res in results:
            try:
                self._book_result(res, opp)
            except Exception as exc:
                # Bookkeeping must never propagate: the order is already on the
                # exchange, and unwinding the round's claim here would let the
                # next poll buy it a second time.
                self.log(f"[crypto] BOOKING ERROR [{res.get('account_name')}]: {exc}")
                self._broker_log(
                    f"Crypto order booked on the exchange but not recorded locally "
                    f"[{res.get('account_name')}] ({opp.asset} {opp.side}, order "
                    f"{res.get('order_id')}): {exc}. Reconcile this position.",
                    level="ERROR",
                )
        return entered_accounts

    def _book_result(self, res: Dict[str, Any], opp) -> None:
        """Records one account's execution result in the local book."""
        name = res["account_name"]
        if res["success"]:
            acc_settings = settings_manager.get_account_settings(name)
            limits = {
                "max_open_positions": acc_settings.get("max_open_positions"),
                "max_total_exposure": acc_settings.get("max_total_exposure"),
                "max_trades_per_day": acc_settings.get("max_trades_per_day"),
            }
            pos, book_reason = self.broker.open_position(
                opp,
                stake=res["stake"],
                mode="LIVE",
                account_name=name,
                wallet_address=res["wallet"],
                limits=limits,
                filled_size=res["filled_size"],
                fill_price=res["avg_price"],
                order_id=res.get("order_id"),
                force=True,
            )
            self.log(
                f"[crypto] LIVE FILL [{name}] {opp.asset} {opp.side} "
                f"{res['filled_size']:.2f} shares @ {res['avg_price']:.4f} "
                f"(cost ${res['filled_cost']:.2f}, order {res.get('order_id')})"
            )
            if pos is None:
                self.log(f"[crypto] LIVE TRACK FAIL [{name}] -- {book_reason}")
                self._broker_log(
                    f"Crypto fill could not be tracked [{name}]: {book_reason}", level="ERROR"
                )
        elif res["resting"]:
            # An order exists against this round even though nothing filled.
            # The round stays claimed so a later poll cannot double it up.
            self.broker.record_unfilled()
            self.log(
                f"[crypto] LIVE OPEN [{name}] {opp.asset} {opp.side} -- {res['error']}"
            )
            self._broker_log(
                f"Crypto order resting unfilled [{name}] (order {res.get('order_id')}): "
                f"{opp.asset} {opp.side} {opp.slug}. Not tracked as a position; cancel it from "
                f"the dashboard if you no longer want the entry.",
                level="WARNING",
            )
        else:
            self.broker.record_rejection()
            self.log(f"[crypto] LIVE ERR [{name}] {opp.asset} {opp.side} -- {res['error']}")
            self._broker_log(f"Crypto order failed [{name}]: {res['error']}", level="ERROR")

    def _execute_paper(self, opp, price: float, cfg: Dict[str, Any]) -> int:
        stake = cfg["stake_per_trade"]
        ok, reason = self.broker.can_open(stake)
        if not ok:
            self.broker.record_intention()
            self.broker.record_rejection()
            self.log_skip(
                f"[crypto] SKIP {opp.asset} {opp.side}: {reason}",
                throttle_key=f"{opp.round_key}:risk:paper",
            )
            return 0
        position, why = self.broker.open_position(opp, stake=stake, mode="PAPER")
        if position is None:
            self.log_skip(
                f"[crypto] SKIP {opp.asset} {opp.side}: {why}",
                throttle_key=f"{opp.round_key}:open:paper",
            )
            return 0
        self.log(
            f"[crypto] PAPER BUY {opp.asset} {opp.side} {opp.slug or opp.market_id} "
            f"@ {price:.4f} stake=${position['stake']:.2f} ({opp.seconds_remaining:.1f}s left)"
        )
        return 1

    # --- one pass ---------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> CryptoPassResult:
        """Runs a single crypto scan-and-trade pass and returns what it did."""
        result = CryptoPassResult()
        settings = settings_manager.load_settings()
        cfg = crypto_scanner.crypto_settings(settings)

        if not cfg["enabled"]:
            result.reasons.append("crypto_disabled")
            return result
        if cfg["status"] == "PAUSED":
            self.log_skip("[crypto] strategy is PAUSED; not scanning.", throttle_key="paused")
            result.reasons.append("crypto_paused")
            return result

        # Re-read state before deciding anything: the dashboard and the sports
        # loop write to the same file, and a stale snapshot would mis-count both
        # the round ledger and the risk budget.
        self.broker.reload()

        scan = crypto_scanner.find_crypto_opportunities(
            client=self.client,
            settings_override=settings,
            now=now,
            claimed_round_keys=self.broker.claimed_crypto_round_keys(),
            held_token_ids=self.broker.held_token_ids,
            log=lambda msg, key: self.log_skip(msg, throttle_key=key),
        )
        result.scanned = scan.rounds_seen
        result.signals = len(scan.opportunities)
        result.skipped = len(scan.skipped)
        result.error = scan.error
        result.reasons.extend(s.reason for s in scan.skipped)

        self.broker.save_crypto_signals(scan.opportunities)

        if cfg["kill_switch"]:
            if scan.opportunities:
                self.log_skip(
                    "[crypto] ENTRY KILL SWITCH ACTIVE: blocking all crypto entries.",
                    throttle_key="kill_switch",
                )
            result.reasons.append("crypto_kill_switch")
            return result

        live = self._live_broker(settings)

        for opp in scan.opportunities:
            # 1. Claim the round before anything else can act on it.
            if not self.broker.claim_crypto_round(opp.round_key, metadata={
                "asset": opp.asset,
                "side": opp.side,
                "market_id": opp.market_id,
                "slug": opp.slug,
                "token_id": opp.token_id,
                "round_end": opp.end_date,
            }):
                self.log_skip(
                    f"[crypto] SKIP {opp.asset} {opp.side} {opp.slug or opp.market_id}: "
                    f"already_traded_this_round",
                    throttle_key=f"{opp.round_key}:claimed",
                )
                result.reasons.append("already_traded_this_round")
                continue

            entered = 0
            try:
                # 2. Strategy budget, then 3. a full re-check against live data.
                budget_ok, budget_reason = self._crypto_budget_ok(cfg, cfg["stake_per_trade"])
                if not budget_ok:
                    self.log_skip(
                        f"[crypto] SKIP {opp.asset} {opp.side}: {budget_reason}",
                        throttle_key=f"{opp.round_key}:budget",
                    )
                    result.reasons.append(REASON_RISK_BLOCKED)
                    result.rejected += 1
                    continue

                ok, reason, detail, price = self.recheck(opp, cfg, now=now)
                if not ok:
                    self.log_skip(
                        f"[crypto] SKIP {opp.asset} {opp.side} {opp.slug or opp.market_id}: "
                        f"{reason} ({detail})",
                        throttle_key=f"{opp.round_key}:{opp.side}:{reason}",
                    )
                    result.reasons.append(reason)
                    result.rejected += 1
                    continue

                # 4. Submit at the price the re-check just confirmed, not the
                # one the scan first saw.
                opp.confirmed_price = float(price)
                if live is not None:
                    entered = self._execute_live(live, opp, price, cfg)
                else:
                    entered = self._execute_paper(opp, price, cfg)
            except Exception as exc:
                self.log(f"[crypto] ERROR entering {opp.asset} {opp.side}: {exc}")
                self._broker_log(f"Crypto entry error on {opp.asset} {opp.side}: {exc}", level="ERROR")
            finally:
                # 5. Keep the claim only if an order actually reached the exchange.
                if entered:
                    self.broker.confirm_crypto_round(opp.round_key, metadata={"accounts": entered})
                    self.broker.record_crypto_trade()
                    result.entries += 1
                    result.entered_round_keys.append(opp.round_key)
                else:
                    self.broker.release_crypto_round(opp.round_key)

        return result

    # --- loop -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Polls on the crypto cadence until stopped.

        The interval is re-read every pass so it can be tightened from the
        dashboard mid-session, and it is floored at 1s so a bad setting cannot
        turn this into a busy loop against the API.
        """
        self.log("[crypto] Crypto 5-Minute strategy loop started.")
        while not self._stop.is_set():
            interval = 3.0
            try:
                cfg = crypto_scanner.crypto_settings()
                interval = max(1.0, float(cfg["poll_interval_seconds"]))
                if not cfg["enabled"]:
                    # Nothing to do, but stay responsive to the setting being
                    # switched on without burning requests in the meantime.
                    self._stop.wait(max(interval, 5.0))
                    continue
                pass_result = self.run_once()
                if pass_result.error:
                    self.log(f"[crypto] scan error: {pass_result.error}")
            except Exception as exc:
                self.log(f"[crypto] ERROR in loop: {exc}")
                self._broker_log(f"Crypto loop error: {exc}", level="ERROR")
            self._stop.wait(interval)
        self.log("[crypto] Crypto 5-Minute strategy loop stopped.")


def start_crypto_thread(broker, log: Optional[Callable[[str], None]] = None) -> Tuple[CryptoStrategy, threading.Thread]:
    """Starts the crypto loop on its own daemon thread.

    A separate thread is what keeps the two cadences independent: crypto can
    poll every 2-5 seconds for a 30-second window while the sports loop keeps
    its own (much slower) scan interval, with neither waiting on the other.
    """
    strategy = CryptoStrategy(broker, log=log)
    thread = threading.Thread(target=strategy.run_forever, name="crypto-5m", daemon=True)
    thread.start()
    return strategy, thread
