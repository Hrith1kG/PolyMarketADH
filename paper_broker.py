"""Simulated portfolio and state manager with daily trade limits,
signals cache, and activity logging for the Streamlit dashboard."""
import errno
import json
import os
import threading
import time
from contextlib import contextmanager
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


# ---------------------------------------------------------------------------
# Cross-process state locking.
#
# state.json has (at least) three writers: the bot loop in main.py, the Streamlit
# dashboard process, and the PaperBroker that live_broker.reconcile_positions
# builds for itself. save() serialises the WHOLE state dict, so without a lock and
# a re-read the last full-file write silently wins -- which is how positions the
# dashboard had just exited came back a minute later, restored from the bot's
# long-lived in-memory snapshot.
#
# Every mutation now runs inside _transaction(): take the lock, re-read from disk,
# apply the change, write, release. The lock is re-entrant within a process so
# nested mutations (e.g. _close_position -> add_log) don't deadlock.
# ---------------------------------------------------------------------------

_LOCK_ACQUIRE_TIMEOUT = 10.0    # seconds to wait for another process to finish
_LOCK_STALE_AFTER = 30.0        # seconds before a lock file is treated as abandoned
_thread_locks: Dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()
_lock_depth: Dict[str, int] = {}


def _thread_lock_for(path: str) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[path] = lock
        return lock


def _acquire_file_lock(lock_path: str) -> Optional[int]:
    """Creates an exclusive lock file, waiting for a concurrent holder to release it.
    Returns the fd, or None if the lock could not be taken (in which case the caller
    proceeds unlocked rather than losing the write outright)."""
    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                return None
            # Reclaim a lock left behind by a process that died mid-write.
            try:
                if (time.time() - os.path.getmtime(lock_path)) > _LOCK_STALE_AFTER:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                print(f"[broker] Timed out waiting for {lock_path}; proceeding without the lock.")
                return None
            time.sleep(0.05)


@contextmanager
def _state_lock(state_path: str):
    """Re-entrant, cross-process lock guarding one state file."""
    lock_path = f"{state_path}.lock"
    thread_lock = _thread_lock_for(state_path)
    with thread_lock:
        depth = _lock_depth.get(state_path, 0)
        fd = _acquire_file_lock(lock_path) if depth == 0 else None
        _lock_depth[state_path] = depth + 1
        try:
            yield
        finally:
            _lock_depth[state_path] = depth
            if depth == 0 and fd is not None:
                try:
                    os.close(fd)
                    os.unlink(lock_path)
                except OSError:
                    pass


_synced_to_db = False  # sync_from_state is a one-time JSON->SQLite migration; running it on
                       # every PaperBroker() (i.e. every Streamlit rerun) re-inserts every
                       # trade and re-resolves any missing slug over the network each time.


class PaperBroker:
    def __init__(self, state_path=None):
        self.state_path = state_path or config.STATE_FILE
        self._txn_depth = 0
        self.state = self._load()
        global _synced_to_db
        if not _synced_to_db:
            database.sync_from_state(self.state)
            _synced_to_db = True

    @contextmanager
    def _transaction(self):
        """Runs a mutation under the state lock against a freshly re-read state,
        writing once on exit. Nested transactions join the outer one, so a compound
        operation is written exactly once and cannot half-apply."""
        with _state_lock(self.state_path):
            outermost = self._txn_depth == 0
            if outermost:
                self.state = self._load()
            self._txn_depth += 1
            try:
                yield self.state
            finally:
                self._txn_depth -= 1
            if outermost:
                self._write()

    def reload(self) -> Dict[str, Any]:
        """Re-reads state.json from disk, discarding this instance's snapshot.

        The bot loop must call this each iteration: it holds one PaperBroker for the
        whole process lifetime, so without it the loop keeps overwriting state.json
        with a snapshot taken before the dashboard's exits and settlements."""
        with _state_lock(self.state_path):
            self.state = self._load()
        return self.state

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
        """Writes the current in-memory state, taking the lock first. Prefer
        _transaction() for mutations -- this overwrites whatever is on disk and is
        only safe when the caller deliberately owns the whole state (e.g. a reset)."""
        with _state_lock(self.state_path):
            self._write()

    def _write(self):
        """Serialises state to disk. Assumes the caller already holds the lock."""
        try:
            tmp = f"{self.state_path}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[broker] Error writing state tmp file: {e}")
            return

        # On Windows, os.replace() can raise PermissionError ("Access is denied") if
        # another process (the bot and the dashboard both run as separate services
        # against the same state.json) briefly has the destination file open -- unlike
        # POSIX, Windows won't rename over a file with an open handle. These collisions
        # are normally released within milliseconds, so retry briefly before giving up
        # and losing the write.
        last_exc = None
        for attempt in range(5):
            try:
                os.replace(tmp, self.state_path)
                return
            except PermissionError as e:
                last_exc = e
                time.sleep(0.1 * (attempt + 1))
        print(f"[broker] Error saving state after retries (state change may be lost): {last_exc}")

    def add_log(self, message: str, level: str = "INFO"):
        entry = {
            "timestamp": _now_iso(),
            "level": level,
            "message": message,
        }
        with self._transaction() as state:
            logs = state.setdefault("logs", [])
            logs.append(entry)
            if len(logs) > 100:
                state["logs"] = logs[-100:]

    def save_signals(self, opportunities):
        payload = [o.to_dict() if hasattr(o, "to_dict") else dict(o) for o in opportunities]
        # Runs as a transaction: this is called every poll by the bot, and writing the
        # whole state from a stale snapshot here is what used to resurrect positions
        # the dashboard had already closed.
        with self._transaction() as state:
            state["signals"] = payload

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
            with self._transaction() as state:
                state["daily_trades"] = {"date": _today_str(), "count": 0}
            return 0
        return dt.get("count", 0)

    def positions_for_account(self, account_name: str) -> Dict[str, Any]:
        """Positions belonging to one account only, for independent per-account risk scoping."""
        return {k: p for k, p in self.state.get("positions", {}).items() if p.get("account_name") == account_name}

    def _today_trades_count_for_account(self, account_name: str) -> int:
        per_acc = self.state.setdefault("daily_trades_by_account", {})
        dt = per_acc.get(account_name, {})
        if dt.get("date") != _today_str():
            with self._transaction() as state:
                state.setdefault("daily_trades_by_account", {})[account_name] = {"date": _today_str(), "count": 0}
            return 0
        return dt.get("count", 0)

    def _increment_daily_trades_for_account(self, account_name: str):
        per_acc = self.state.setdefault("daily_trades_by_account", {})
        dt = per_acc.get(account_name, {})
        if dt.get("date") != _today_str():
            dt = {"date": _today_str(), "count": 0}
        dt["count"] = dt.get("count", 0) + 1
        per_acc[account_name] = dt

    def can_open(
        self,
        stake: float,
        account_name: Optional[str] = None,
        limits: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Checks risk limits against either the whole portfolio, or (when account_name is
        given) just that account's own positions/trades/exposure -- so accounts can run
        independent risk budgets. `limits` overrides individual caps for that account;
        any cap left out falls back to the global setting."""
        settings = settings_manager.load_settings()
        limits = limits or {}

        # An explicit per-account limit of 0 is a real setting ("this account may not
        # trade"), not an absent one -- `or` treated it as falsy and silently swapped
        # in the global cap, so a cap of 0 could not be expressed.
        def _limit(key: str, default):
            value = limits.get(key)
            if value is None:
                value = settings.get(key, default)
            return value

        max_positions = _limit("max_open_positions", 10)
        max_exposure = _limit("max_total_exposure", 200.0)
        max_daily_trades = _limit("max_trades_per_day", 10)

        if account_name:
            scoped_positions = self.positions_for_account(account_name)
            today_count = self._today_trades_count_for_account(account_name)
            exposure = sum(p["stake"] for p in scoped_positions.values())
        else:
            # PAPER scope. state.json holds LIVE and PAPER positions together, so
            # counting both here let live positions consume the paper caps and vice
            # versa; scope the paper budget to paper positions only.
            scoped_positions = {
                k: p for k, p in self.state.get("positions", {}).items()
                if str(p.get("mode", "PAPER")).upper() == "PAPER"
            }
            today_count = self.today_trades_count
            exposure = sum(p["stake"] for p in scoped_positions.values())

        if today_count >= max_daily_trades:
            return False, f"max daily trades limit reached ({today_count}/{max_daily_trades})"

        if len(scoped_positions) >= max_positions:
            return False, f"max open positions reached ({len(scoped_positions)}/{max_positions})"

        if exposure + stake > max_exposure:
            return False, f"would exceed max exposure (${exposure + stake:.2f} > ${max_exposure:.2f})"

        # The simulated balance is a PAPER concept. Applying it to LIVE entries meant
        # real trading was throttled by a fictional $1,000 ledger that had nothing to
        # do with wallet USDC -- and that phantom settlements kept inflating. Live
        # capital is constrained by the exposure cap above and by the wallet itself.
        if account_name is None and stake > self.state["balance"]:
            return False, f"insufficient balance (${self.state['balance']:.2f} < ${stake:.2f})"

        return True, ""

    def _bump_lifecycle(self, key: str, amount: int = 1):
        with self._transaction() as state:
            lc = state.setdefault("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0})
            lc[key] = lc.get(key, 0) + amount

    def record_intention(self):
        self._bump_lifecycle("intentions")

    def record_rejection(self):
        self._bump_lifecycle("rejected")

    def record_unfilled(self):
        """An order the exchange accepted but that is resting unfilled. Counted under
        `pending` -- a lifecycle bucket that existed in the schema but was never
        incremented, because unfilled orders used to be booked as filled positions."""
        self._bump_lifecycle("pending")

    def open_position(
        self,
        opp,
        stake: Optional[float] = None,
        mode: str = "PAPER",
        account_name: str = config.DEFAULT_ACCOUNT_NAME,
        wallet_address: Optional[str] = None,
        limits: Optional[Dict[str, Any]] = None,
        filled_size: Optional[float] = None,
        fill_price: Optional[float] = None,
        order_id: Optional[str] = None,
        force: bool = False,
        **kwargs
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Records a position that has actually been acquired.

        For LIVE mode the caller must pass the real fill from the exchange --
        `filled_size` (shares) and `fill_price` (realized average) -- so the local
        book reflects what executed rather than what was requested. A partial fill
        produces a partial position.

        `force=True` skips the risk gate. It exists for one situation only: the order
        already executed on-chain, so refusing to record it would leave real exposure
        completely untracked. It logs loudly instead.
        """
        settings = settings_manager.load_settings()
        stake = stake if stake is not None else settings.get("stake_per_trade", 25.0)

        self.record_intention()
        scope_account = account_name if mode.upper() == "LIVE" else None
        ok, reason = self.can_open(stake, account_name=scope_account, limits=limits)
        if not ok:
            if not force:
                self.record_rejection()
                return None, reason
            self.add_log(
                f"RISK OVERRIDE [{mode.upper()}][{account_name}]: recording an already-executed "
                f"order that breaches a limit ({reason}). Untracked live exposure is worse than a "
                f"breached cap -- review this position.",
                level="WARNING",
            )

        # Size the position from the actual fill when one was reported.
        entry_price = float(fill_price) if fill_price else float(opp.confirmed_price)
        if filled_size is not None and float(filled_size) > 0:
            shares = float(filled_size)
            stake = shares * entry_price
        else:
            shares = stake / entry_price

        clean_acc = "".join(c for c in account_name if c.isalnum() or c in ("_", "-"))
        # trade_id must be unique per execution. It used to be
        # trd_ord_{account}_{token[:8]}, which collides on every re-entry into the
        # same market -- and database.record_trade does INSERT OR REPLACE, so the
        # earlier (possibly already settled) row was silently destroyed.
        opened_at = _now_iso()
        trade_id = f"trd_ord_{clean_acc}_{str(opp.token_id)[:8]}_{int(time.time() * 1000)}"
        pos_key = f"{clean_acc}_{opp.token_id}" if account_name != config.DEFAULT_ACCOUNT_NAME else str(opp.token_id)
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
            "entry_price": entry_price,
            "quoted_price": float(opp.confirmed_price),
            "order_id": order_id,
            "shares": shares,
            "stake": stake,
            "potential_payout": shares * 1.0,
            "potential_profit": (shares * 1.0) - stake,
            "time_left": time_left_str,
            "opened_at": opened_at,
            "end_date": opp.end_date,
            "game_start_time": getattr(opp, "game_start_time", None),
        }

        with self._transaction() as state:
            state.setdefault("positions", {})[pos_key] = position
            # The simulated cash ledger tracks PAPER only. Debiting it for LIVE
            # trades made a fictional balance gate real execution.
            if mode.upper() == "PAPER":
                state["balance"] = state.get("balance", config.STARTING_BALANCE) - stake

            lc = state.setdefault("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0})
            lc["filled"] = lc.get("filled", 0) + 1

            dt = state.setdefault("daily_trades", {"date": _today_str(), "count": 0})
            if dt.get("date") != _today_str():
                dt["date"] = _today_str()
                dt["count"] = 1
            else:
                dt["count"] = dt.get("count", 0) + 1

            if mode.upper() == "LIVE":
                self._increment_daily_trades_for_account(account_name)

        # Record into local SQLite database
        database.record_trade({
            "trade_id": trade_id,
            "placed_at": opened_at,
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

        self.add_log(f"OPENED [{mode.upper()}][{account_name}]: {opp.question[:45]} [{opp.outcome_label}] {shares:.2f} shares @ {entry_price:.4f} cost=${stake:.2f}")
        return position, ""

    def _close_position(self, pos_key: str, resolved_price: float, note: str) -> Dict[str, Any]:
        """Books a settlement/exit in state.json AND trades.db as one unit, so the two
        stores cannot disagree about whether a position is still open."""
        with self._transaction() as state:
            position = state["positions"].pop(pos_key)
            payout = position["shares"] * resolved_price
            pnl = payout - position["stake"]
            closed_at = _now_iso()
            trade = {
                **position,
                "closed_at": closed_at,
                "resolved_price": resolved_price,
                "payout": payout,
                "pnl": pnl,
                "note": note,
            }
            state.setdefault("closed_trades", []).append(trade)
            # Credit the simulated ledger for PAPER only, mirroring the debit in
            # open_position -- live payouts land in the real wallet, not here.
            if str(position.get("mode", "PAPER")).upper() == "PAPER":
                state["balance"] = state.get("balance", 0.0) + payout

            acc_str = f"[{position.get('account_name', config.DEFAULT_ACCOUNT_NAME)}] " if position.get('account_name') else ""
            self.add_log(f"SETTLED {acc_str}: {position['question'][:45]} [{position['outcome_label']}] pnl={pnl:+.2f} ({note})")

            # Settle in local SQLite database. Break-even is EVEN, not LOST -- the two
            # stores used to classify it differently, which skewed the win rate.
            database.settle_trade(
                token_id=str(position.get("token_id", pos_key)),
                trade_id=position.get("trade_id"),
                resolved_price=resolved_price,
                payout=payout,
                pnl=pnl,
                result=database.classify_result(pnl),
                closed_at=closed_at,
                note=note,
            )

        return trade

    def _find_position_key(self, identifier: str) -> Optional[str]:
        """Resolves a position key, token id or trade id to a state.json position key."""
        positions = self.state.get("positions", {})
        if identifier in positions:
            return identifier
        for k, p in positions.items():
            if str(p.get("token_id")) == str(identifier) or str(p.get("trade_id")) == str(identifier):
                return k
        return None

    def discard_position(self, identifier: str, note: str = "", closed_price: Optional[float] = None) -> bool:
        """Removes a tracked position that reconciliation has established is not real
        (never filled) or is already closed on Polymarket.

        Every "already exited" path used to write only to trades.db, leaving the
        state.json entry untouched -- so the position kept rendering in Open Positions
        no matter how many times it was exited. This is the state.json half.

        When `closed_price` is None the position is treated as VOID: it never existed,
        so it is not appended to closed_trades and books no P&L. A PAPER void refunds
        the stake that open_position debited.
        """
        with self._transaction() as state:
            key = self._find_position_key(identifier)
            if key is None:
                return False
            position = state["positions"].pop(key)
            is_paper = str(position.get("mode", "PAPER")).upper() == "PAPER"

            if closed_price is None:
                if is_paper:
                    state["balance"] = state.get("balance", 0.0) + float(position.get("stake", 0.0))
                self.add_log(
                    f"VOIDED [{position.get('account_name', '')}]: {str(position.get('question', ''))[:45]} "
                    f"[{position.get('outcome_label', '')}] -- {note or 'never executed'}",
                    level="WARNING",
                )
                return True

            payout = float(position.get("shares", 0.0)) * float(closed_price)
            pnl = payout - float(position.get("stake", 0.0))
            if is_paper:
                state["balance"] = state.get("balance", 0.0) + payout
            state.setdefault("closed_trades", []).append({
                **position,
                "closed_at": _now_iso(),
                "resolved_price": float(closed_price),
                "payout": payout,
                "pnl": pnl,
                "note": note,
            })
            self.add_log(
                f"RECONCILED [{position.get('account_name', '')}]: {str(position.get('question', ''))[:45]} "
                f"pnl={pnl:+.2f} ({note})"
            )
            return True

    def force_settle_position(self, token_id_or_key: str, won: bool = True, note: str = "") -> Optional[Dict[str, Any]]:
        """Manually settles an open position as WON (1.0) or LOST (0.0), booking P&L and updating SQLite DB."""
        settle_price = 1.0 if won else 0.0
        settle_note = note or ("manual settlement: WON" if won else "manual settlement: LOST")
        # Resolve the key inside the transaction so it is looked up against the state
        # actually on disk, not a snapshot another process may have moved on from.
        with self._transaction():
            target_key = self._find_position_key(token_id_or_key)
            if not target_key:
                return None
            return self._close_position(target_key, settle_price, settle_note)

    def exit_position(self, token_id_or_key: str, exit_price: float, note: str = "") -> Optional[Dict[str, Any]]:
        """Manually exits/sells an open position at a specified exit price, booking P&L and updating SQLite DB."""
        settle_note = note or f"Manual exit @ ${exit_price:.4f}"
        with self._transaction():
            target_key = self._find_position_key(token_id_or_key)
            if not target_key:
                return None
            return self._close_position(target_key, float(exit_price), settle_note)

    def reset_paper_portfolio(self) -> int:
        """Clears PAPER positions/history and restores the simulated balance,
        leaving LIVE positions tracked. Returns how many live positions were kept.

        The dashboard's reset used to replace `positions` with {} wholesale, which
        also deleted the tracking for real on-chain positions.
        """
        with self._transaction() as state:
            kept = {
                k: p for k, p in state.get("positions", {}).items()
                if str(p.get("mode", "PAPER")).upper() == "LIVE"
            }
            state["positions"] = kept
            state["closed_trades"] = [
                t for t in state.get("closed_trades", [])
                if str(t.get("mode", "PAPER")).upper() == "LIVE"
            ]
            state["balance"] = config.STARTING_BALANCE
            state["signals"] = []
            state["daily_trades"] = {"date": _today_str(), "count": 0}
            state["order_lifecycle"] = {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0}
            state.setdefault("logs", []).append({
                "timestamp": _now_iso(),
                "level": "INFO",
                "message": f"Paper portfolio reset by user ({len(kept)} live position(s) retained).",
            })
            return len(kept)

    def check_resolutions(self) -> List[Dict[str, Any]]:
        """Settles open positions whose true outcome can be established.

        Settlement is only ever booked from a price observed for OUR outcome token.
        The previous version had two paths that fabricated wins:

          * `1.0 if raw_price >= 0.5 or is_uma_resolved else 0.0` -- the `or` made
            every UMA-resolved market a win, including ones this side lost.
          * a time-based branch that settled at 1.0 unconditionally once end_date was
            more than 10 hours old, with no outcome check at all.

        Both are gone. When the outcome cannot be determined the position is left
        PENDING and flagged in `settlement_blocked` so the dashboard can surface it
        for manual settlement, rather than being guessed at.
        """
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

            settle_price = None
            note = ""

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

                if raw_price is not None:
                    if is_uma_resolved:
                        settle_price = 1.0 if raw_price >= 0.5 else 0.0
                        note = "settled (win)" if settle_price == 1.0 else "settled (loss)"
                    elif m.state.closed and (raw_price >= 0.95 or raw_price <= 0.05):
                        settle_price = 1.0 if raw_price >= 0.5 else 0.0
                        note = "settled (win)" if settle_price == 1.0 else "settled (loss)"
                    elif m.state.closed:
                        # Closed but not at a decisive price: settle at the last
                        # observed price rather than rounding it to a win.
                        settle_price = raw_price
                        note = f"settled at market close price ${raw_price:.4f}"

            if settle_price is not None:
                trade = self._close_position(pos_key, settle_price, note)
                settled.append(trade)
                continue

            # Could not establish the outcome. Flag it instead of inventing one.
            blocked_reason = None
            end_date_str = position.get("end_date")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
                    if (now_utc - end_dt) > timedelta(hours=2):
                        blocked_reason = (
                            f"Resolution deadline passed {str(end_dt)[:16]} but no confirmed "
                            f"outcome is available for this token yet. Needs manual settlement "
                            f"or on-chain reconciliation."
                        )
                except Exception as exc:
                    print(f"[paper_broker] Error checking end_date for {token_id}: {exc}")

            if blocked_reason and position.get("settlement_blocked") != blocked_reason:
                with self._transaction() as state:
                    tracked = state.get("positions", {}).get(pos_key)
                    if tracked is not None:
                        tracked["settlement_blocked"] = blocked_reason
                self.add_log(
                    f"UNRESOLVED [{position.get('account_name', '')}]: "
                    f"{str(position.get('question', ''))[:45]} -- {blocked_reason}",
                    level="WARNING",
                )

        return settled

    def summary(self, mode_filter: Optional[str] = None) -> Dict[str, Any]:
        """Returns portfolio KPIs. Pass mode_filter="LIVE" or "PAPER" to scope every
        figure to positions/trades recorded under that mode only -- both modes share
        the same state.json, so without this the numbers silently blend LIVE and
        PAPER activity together regardless of which mode is currently selected."""
        positions = self.state.get("positions", {})
        closed = self.state.get("closed_trades", [])
        if mode_filter:
            positions = {
                k: p for k, p in positions.items()
                if str(p.get("mode", "PAPER")).upper() == mode_filter.upper()
            }
            closed = [t for t in closed if str(t.get("mode", "PAPER")).upper() == mode_filter.upper()]

        # Voided rows never executed -- they are not trades and must not sit in the
        # win-rate denominator. Break-even is counted separately from a loss.
        closed = [t for t in closed if str(t.get("result", "")).upper() != "VOID"]
        wins = [t for t in closed if database.classify_result(float(t.get("pnl", 0) or 0)) == "WON"]
        losses = [t for t in closed if database.classify_result(float(t.get("pnl", 0) or 0)) == "LOST"]
        win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
        open_exposure = sum(p["stake"] for p in positions.values())
        return {
            "balance": self.state.get("balance", config.STARTING_BALANCE),
            "open_positions": len(positions),
            "open_exposure": open_exposure,
            # Scoped like every other figure here. LIVE keeps a per-account tally,
            # so sum those; PAPER uses the global counter.
            "today_trades": (
                sum(
                    v.get("count", 0)
                    for v in self.state.get("daily_trades_by_account", {}).values()
                    if v.get("date") == _today_str()
                )
                if str(mode_filter or "").upper() == "LIVE"
                else self.today_trades_count
            ),
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "realized_pnl": sum(t.get("pnl", 0) for t in closed),
            "reserved_capital": open_exposure,
            "order_lifecycle": self.state.get("order_lifecycle", {"intentions": 0, "pending": 0, "filled": 0, "rejected": 0}),
        }

    def restore_position_from_trade(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Recreates an open position in state.json from a SQLite trade row that has
        no matching entry there (e.g. state.json was reset/edited after the trade was
        placed, orphaning its still-PENDING row). Used by the dashboard's orphaned-trade
        reconciliation tool when the user decides a stale row actually represents a
        position that should still be tracked as open."""
        pos_key = str(trade.get("token_id") or trade.get("trade_id"))
        position = {
            "trade_id": trade.get("trade_id"),
            "mode": str(trade.get("broker", "paper")).upper(),
            "account_name": trade.get("account_name", config.DEFAULT_ACCOUNT_NAME),
            "wallet_address": trade.get("wallet_address", ""),
            "token_id": trade.get("token_id"),
            "market_id": trade.get("market_id"),
            "slug": trade.get("slug", ""),
            "question": trade.get("question", ""),
            "outcome_label": trade.get("outcome", ""),
            "entry_price": trade.get("entry_price", 0.0),
            "shares": trade.get("tokens", 0.0),
            "stake": trade.get("cost", 0.0),
            "potential_payout": trade.get("tokens", 0.0),
            "potential_profit": float(trade.get("tokens", 0.0)) - float(trade.get("cost", 0.0)),
            "time_left": trade.get("time_left", "0.0m"),
            "opened_at": trade.get("placed_at", _now_iso()),
            "end_date": None,
            "game_start_time": None,
        }
        self.state["positions"][pos_key] = position
        self.save()
        return position

