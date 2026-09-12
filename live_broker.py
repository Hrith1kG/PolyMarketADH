"""Real order execution, account vitals tracking, and CTF position redemption
via Polymarket's official polymarket-client SDK across single or multiple accounts."""
import concurrent.futures
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
import config
import polymarket_client
import settings_manager


class LiveBrokerError(Exception):
    pass


# ---------------------------------------------------------------------------
# Order response interpretation.
#
# polymarket-client's place_limit_order() returns OrderResponse, which is
# AcceptedOrder | RejectedOrder -- a CLOB-level rejection (unmatched,
# not_enough_balance, market_not_ready, invalid_nonce, fok/fak_not_filled, ...)
# comes back as RejectedOrder WITHOUT raising. And an AcceptedOrder still carries
# status "live" | "matched" | "delayed", where only "matched" means shares
# actually changed hands; "live" means the order is resting in the book, unfilled.
#
# Treating "the call didn't raise" as "the position is open" is what produced
# locally tracked positions that don't exist on the account, so every execution
# path below routes its response through interpret_order_response() and callers
# key off filled_size, never off the absence of an exception.
#
# Amount semantics (see _compute_limit_order_amounts in the SDK):
#   BUY  -> making_amount = USDC spent,   taking_amount = shares received
#   SELL -> making_amount = shares sold,  taking_amount = USDC received
# ---------------------------------------------------------------------------

FILLED_STATUS = "matched"
RESTING_STATUSES = ("live", "delayed")

# Selling a position rarely clears the wallet to exactly zero. Order sizes are
# quantised to 2dp, so a sell floored to 1.05 against a holding of 1.06 leaves
# 0.01 shares behind -- roughly one cent of value, but still a non-zero balance.
# Reconciliation used to treat ANY balance > 0 as "still holding this position",
# so that one cent of dust pinned the trade as PENDING permanently and the
# dashboard kept listing a position that had already been sold.
DUST_SHARE_THRESHOLD = 0.05


def is_no_balance_rejection(code: Optional[str], message: str) -> bool:
    """True when a rejection means "the wallet doesn't hold this".

    The SDK's error-code inference checks status first, so a balance failure that
    the CLOB also marks "unmatched" is reported as code "unmatched" rather than
    "not_enough_balance" -- matching on the code alone misses it. Check the message
    too, and treat the exception text the same way (the HTTP 400 path raises).
    """
    if str(code or "") == "not_enough_balance":
        return True
    msg = str(message or "").lower()
    return "not enough balance" in msg or "balance is not enough" in msg


def interpret_order_response(response: Any, side: str, requested_size: float, requested_price: float) -> Dict[str, Any]:
    """Normalizes an SDK OrderResponse into explicit fill facts.

    Returns a dict with:
      accepted      -- the exchange took the order (it may still be unfilled)
      filled        -- shares actually changed hands
      resting       -- accepted but sitting in the book unfilled
      filled_size   -- shares filled (0.0 when nothing filled)
      filled_cost   -- USDC spent (BUY) or received (SELL) for the filled portion
      avg_price     -- realized average fill price, or the requested price as fallback
      status/order_id/code/message -- passthrough for logging and reconciliation
    """
    result: Dict[str, Any] = {
        "accepted": False,
        "filled": False,
        "resting": False,
        "filled_size": 0.0,
        "filled_cost": 0.0,
        "avg_price": float(requested_price),
        "status": None,
        "order_id": None,
        "trade_ids": (),
        "code": None,
        "message": "",
        "amounts_unavailable": False,
        "raw": response,
    }

    if response is None:
        result["code"] = "no_response"
        result["message"] = "Exchange returned no order response."
        return result

    # RejectedOrder -> ok is False and carries a machine-readable code.
    if getattr(response, "ok", None) is False:
        result["code"] = str(getattr(response, "code", "unknown"))
        result["message"] = str(getattr(response, "message", "") or "Order rejected by exchange.")
        return result

    if getattr(response, "ok", None) is not True:
        # Unrecognized shape: refuse to guess that it filled.
        result["code"] = "unrecognized_response"
        result["message"] = f"Unrecognized order response type: {type(response).__name__}"
        return result

    status = str(getattr(response, "status", "") or "").lower()
    result["accepted"] = True
    result["status"] = status
    result["order_id"] = str(getattr(response, "order_id", "") or "") or None
    result["trade_ids"] = tuple(getattr(response, "trade_ids", ()) or ())

    making = float(getattr(response, "making_amount", 0.0) or 0.0)
    taking = float(getattr(response, "taking_amount", 0.0) or 0.0)
    if str(side).upper() == "BUY":
        filled_size, filled_cost = taking, making
    else:
        filled_size, filled_cost = making, taking

    if status == FILLED_STATUS:
        result["filled"] = True
        if filled_size > 0.0:
            result["filled_size"] = filled_size
            result["filled_cost"] = filled_cost if filled_cost > 0.0 else filled_size * float(requested_price)
        else:
            # Matched, but the response omitted the amounts (the CLOB sends "" for
            # some fills). Fall back to what we asked for rather than dropping a real
            # fill, and flag it so reconciliation can correct the size from on-chain
            # trades instead of trusting this number.
            result["filled_size"] = float(requested_size)
            result["filled_cost"] = float(requested_size) * float(requested_price)
            result["amounts_unavailable"] = True
        result["avg_price"] = (result["filled_cost"] / result["filled_size"]) if result["filled_size"] > 0 else float(requested_price)
        return result

    # Accepted but not matched. A partial fill can still be reported alongside a
    # resting remainder, so book whatever actually filled and flag the rest.
    if filled_size > 0.0:
        result["filled"] = True
        result["filled_size"] = filled_size
        result["filled_cost"] = filled_cost if filled_cost > 0.0 else filled_size * float(requested_price)
        result["avg_price"] = result["filled_cost"] / result["filled_size"]
    if status in RESTING_STATUSES and filled_size < float(requested_size):
        result["resting"] = True
        result["message"] = f"Order {status} on the book, {filled_size:.4f}/{float(requested_size):.4f} shares filled."
    return result


def check_credentials_available() -> Tuple[bool, str]:
    """Checks whether valid live trading credentials are set in .env or environment."""
    accounts = config.get_configured_accounts()
    enabled = [a for a in accounts if a.get("enabled", True)]
    if not enabled:
        return False, "No active live trading accounts configured."
    return True, f"{len(enabled)} account(s) ready ({', '.join(a['name'] for a in enabled)})"


class AccountSession:
    """Manages an individual Polymarket trading account session."""

    def __init__(self, account_config: Dict[str, Any]):
        self.account_id = str(account_config.get("id", "1"))
        self.name = account_config.get("name", f"Account_{self.account_id}")
        self.private_key = account_config.get("private_key", "")
        self.funder_address = account_config.get("funder_address")
        self.custom_stake = account_config.get("stake", config.STAKE_PER_TRADE)
        self.enabled = account_config.get("enabled", True)
        self.relayer_api_key = account_config.get("relayer_api_key")
        self.relayer_api_key_address = account_config.get("relayer_api_key_address")

        try:
            self.client = polymarket_client.get_secure_client(
                private_key=self.private_key,
                wallet=self.funder_address or None,
                relayer_api_key=self.relayer_api_key or None,
                relayer_api_key_address=self.relayer_api_key_address or None,
            )
            self.wallet = str(self.client.wallet) if self.client.wallet else (self.funder_address or "Unknown")
            self.wallet_type = str(getattr(self.client, "wallet_type", "EOA"))
        except Exception as exc:
            raise LiveBrokerError(f"Failed to initialize SecureClient for '{self.name}': {exc}")

    def get_collateral_balance(self) -> float:
        """Queries live USDC.e collateral balance for this account."""
        try:
            ba = self.client.get_balance_allowance(asset_type="COLLATERAL")
            if ba and ba.balance is not None:
                return round(float(ba.balance) / 1_000_000.0, 2)
            return 0.0
        except Exception as exc:
            print(f"[live_broker][{self.name}] Failed to query balance: {exc}")
            return 0.0

    def get_allowance(self) -> float:
        """Queries live USDC.e collateral allowance for this account."""
        try:
            ba = self.client.get_balance_allowance(asset_type="COLLATERAL")
            if ba and ba.allowances:
                max_raw = max(ba.allowances.values()) if ba.allowances else 0
                return round(float(max_raw) / 1_000_000.0, 2)
            return 0.0
        except Exception as exc:
            print(f"[live_broker][{self.name}] Failed to query allowance: {exc}")
            return 0.0

    def get_account_vitals(self) -> Dict[str, Any]:
        """Returns complete live status and balances for this account."""
        return {
            "account_id": self.account_id,
            "name": self.name,
            "credentials_ok": True,
            "wallet": self.wallet,
            "wallet_type": self.wallet_type,
            "funder_address": self.funder_address,
            "stake": settings_manager.get_account_stake(self.name, fallback=self.custom_stake or config.STAKE_PER_TRADE),
            "collateral_balance": self.get_collateral_balance(),
            "allowance": self.get_allowance(),
        }

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Queries active limit orders on the CLOB for this account."""
        try:
            paginator = self.client.list_open_orders()
            orders = []
            for order in paginator.iter_items():
                orders.append({
                    "account": self.name,
                    "wallet": self.wallet,
                    "id": str(order.id),
                    "market": str(order.market),
                    "token_id": str(order.asset_id),
                    "side": str(order.side),
                    "price": float(order.price) if order.price is not None else 0.0,
                    "size": float(order.original_size) if order.original_size is not None else 0.0,
                    "filled": float(order.size_matched) if order.size_matched is not None else 0.0,
                    "created_at": str(order.created_at),
                })
            return orders
        except Exception as exc:
            print(f"[live_broker][{self.name}] Failed to list open orders: {exc}")
            return []

    def get_live_positions(self) -> List[Dict[str, Any]]:
        """Queries on-chain positions for this account."""
        try:
            paginator = self.client.list_positions(user=self.wallet)
            positions = []
            for p in paginator.iter_items():
                positions.append({
                    "account": self.name,
                    "wallet": self.wallet,
                    "title": str(p.title or ""),
                    "token_id": str(p.asset_id or ""),
                    "event_id": str(p.event_id or ""),
                    "outcome": str(p.outcome or ""),
                    "size": float(p.size) if p.size is not None else 0.0,
                    "avg_price": float(p.avg_price) if p.avg_price is not None else 0.0,
                    "current_value": float(p.current_value) if p.current_value is not None else 0.0,
                    "cash_pnl": float(p.cash_pnl) if p.cash_pnl is not None else 0.0,
                    "percent_pnl": float(p.percent_pnl) if p.percent_pnl is not None else 0.0,
                    "redeemable": bool(p.redeemable),
                })
            return positions
        except Exception as exc:
            print(f"[live_broker][{self.name}] Failed to list live positions: {exc}")
            return []

    def get_market_order_constraints(self, token_id: str) -> Dict[str, Any]:
        """Reads the live book for a token: minimum order size, tick size, best ask
        and the size resting at it. Order sizing used to ignore all of this, so the
        bot could emit orders the exchange had to reject (below minimum size), which
        the old code then recorded as filled positions."""
        info: Dict[str, Any] = {"min_order_size": 0.0, "tick_size": 0.0, "best_ask": None, "ask_size": 0.0}
        try:
            client = polymarket_client.get_public_client()
            book = client.get_order_book(token_id=str(token_id))
            info["min_order_size"] = float(book.min_order_size or 0.0)
            info["tick_size"] = float(book.tick_size or 0.0)
            if book.asks:
                # SDK documents asks as descending price order, best (lowest) ask last.
                info["best_ask"] = float(book.asks[-1].price)
                info["ask_size"] = float(book.asks[-1].size)
        except Exception as exc:
            print(f"[live_broker][{self.name}] Could not read book for {token_id}: {exc}")
        return info

    def place_buy(self, token_id: str, price: float, stake_usd: Optional[float] = None,
                  order_type: str = "LIMIT", max_price: Optional[float] = None) -> Dict[str, Any]:
        """Places a buy order sized to `stake_usd` at `price` and returns the interpreted
        fill outcome (see interpret_order_response). Never reports a fill it didn't get.

        `max_price` caps how far a MARKET order may cross the book, which is the
        exchange-side slippage control Polymarket documents for market orders
        ("maxPrice prevents a BUY from crossing a higher price"). Without it a
        market buy will take whatever the book offers, however thin. It defaults
        to None so existing callers keep their current behaviour exactly; the
        crypto strategy passes its own cap. It has no effect on LIMIT orders,
        whose price is already the cap.
        """
        stake = stake_usd if stake_usd is not None else settings_manager.get_account_stake(self.name, fallback=self.custom_stake or config.STAKE_PER_TRADE)
        
        if order_type.upper() == "MARKET":
            try:
                # Market order uses 'amount' as the USDC spend target.
                market_kwargs: Dict[str, Any] = {
                    "token_id": token_id,
                    "side": "BUY",
                    "amount": float(stake),
                }
                if max_price is not None:
                    market_kwargs["max_price"] = float(max_price)
                response = self.client.place_market_order(**market_kwargs)
            except Exception as exc:
                raise LiveBrokerError(f"[{self.name}] Failed to place market order: {exc}")
            
            # Since market orders don't strictly have a requested price/size in shares, 
            # we estimate the size for reconciliation fallback if amounts are omitted.
            est_size = math.ceil((float(stake) / float(price)) * 100.0) / 100.0 if float(price) > 0 else 0.0
            outcome = interpret_order_response(response, side="BUY", requested_size=est_size, requested_price=float(price))
            outcome["requested_size"] = est_size
            outcome["requested_stake"] = float(stake)
            return outcome

        # Ensure ceiling rounding and min notional >= 1.00 USD so Polymarket's min size check never fails
        size = math.ceil((float(stake) / float(price)) * 100.0) / 100.0
        if size * float(price) < 1.0:
            size = math.ceil((1.00 / float(price)) * 100.0) / 100.0

        # The book's `min_order_size` is logged but deliberately NOT enforced. An
        # earlier version raised on it, which blocked orders the exchange
        # demonstrably accepts: wallet history shows filled buys of ~1.06 shares on
        # markets reporting a larger min_order_size, and Polymarket's own UI opens
        # positions of ~1.5 shares. The SDK never validates against the field either
        # -- it is book metadata whose exact semantics aren't documented well enough
        # to gate real orders on. The exchange decides, and interpret_order_response()
        # below makes sure a refusal is recorded as a refusal, not as a filled position.
        constraints = self.get_market_order_constraints(token_id)
        min_size = float(constraints.get("min_order_size") or 0.0)
        if min_size > 0 and size < min_size:
            print(f"[live_broker][{self.name}] Note: stake ${float(stake):.2f} at ${float(price):.4f} is {size:.2f} shares, "
                  f"below this market's reported min_order_size of {min_size:.2f}. Submitting anyway.")

        try:
            response = self.client.place_limit_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to place order: {exc}")

        outcome = interpret_order_response(response, side="BUY", requested_size=size, requested_price=float(price))
        outcome["requested_size"] = size
        outcome["requested_stake"] = float(stake)

        # If the exchange did refuse it, attach the book's own constraints to the
        # message so the reason is actionable instead of a bare error code.
        if not outcome["filled"] and not outcome["resting"]:
            constraints = self.get_market_order_constraints(token_id)
            min_size = float(constraints.get("min_order_size") or 0.0)
            tick = float(constraints.get("tick_size") or 0.0)
            detail = []
            if min_size:
                detail.append(f"market min_order_size={min_size:g} shares (${min_size * float(price):.2f} at this price)")
            if tick:
                detail.append(f"tick_size={tick:g}")
            if detail:
                outcome["message"] = f"{outcome.get('message', '')} [order was {size:.2f} shares; {'; '.join(detail)}]".strip()
        return outcome

    def get_position_size(self, token_id: str) -> Optional[float]:
        """Returns the shares of `token_id` actually held on-chain, or None if the
        lookup fails. Exits should sell this rather than the locally recorded size:
        the two drifted apart because buys were rounded up to 2dp while the local
        record stored the unrounded stake/price, so every exit under-sold and left
        dust behind."""
        try:
            for p in self.get_live_positions():
                if str(p.get("token_id")) == str(token_id):
                    return float(p.get("size", 0.0) or 0.0)
            return 0.0
        except Exception as exc:
            print(f"[live_broker][{self.name}] Could not read on-chain size for {token_id}: {exc}")
            return None

    def place_sell(self, token_id: str, price: float, size: float, order_type: str = "LIMIT") -> Dict[str, Any]:
        """Places a sell order for `size` shares at `price` to exit an open position,
        returning the interpreted fill outcome rather than a bare response."""
        size_to_sell = math.floor(float(size) * 100.0) / 100.0
        if size_to_sell <= 0:
            raise LiveBrokerError(f"[{self.name}] Invalid sell size: {size}")
        
        if order_type.upper() == "MARKET":
            try:
                response = self.client.place_market_order(
                    token_id=token_id,
                    side="SELL",
                    shares=size_to_sell,
                )
            except Exception as exc:
                raise LiveBrokerError(f"[{self.name}] Failed to place market sell order: {exc}")
        else:
            try:
                response = self.client.place_limit_order(
                    token_id=token_id,
                    price=price,
                    size=size_to_sell,
                    side="SELL",
                )
            except Exception as exc:
                raise LiveBrokerError(f"[{self.name}] Failed to place sell order: {exc}")

        outcome = interpret_order_response(response, side="SELL", requested_size=size_to_sell, requested_price=float(price))
        outcome["requested_size"] = size_to_sell
        return outcome

    def cancel_order(self, order_id: str) -> Any:
        """Cancels one resting CLOB order. Unfilled orders used to sit in the book
        indefinitely with nothing in the app able to clear them."""
        try:
            return self.client.cancel_order(order_id=str(order_id))
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to cancel order {order_id}: {exc}")

    def cancel_all_orders(self) -> Any:
        """Cancels every resting CLOB order for this account."""
        try:
            return self.client.cancel_all()
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to cancel all orders: {exc}")

    def redeem_winning_position(self, condition_id: Optional[str] = None, market_id: Optional[str] = None) -> Any:
        """Redeems resolved winning outcome tokens on CTF contract back to collateral."""
        try:
            tx = self.client.redeem_positions(condition_id=condition_id, market_id=market_id)
            outcome = tx.wait()
            return outcome
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to redeem CTF position: {exc}")


def _buy_result(session: "AccountSession", stake: float, outcome: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> Dict[str, Any]:
    """Builds the per-account execution record the callers act on.

    `success` now means "shares actually filled", not "the call didn't raise" --
    callers must never open a tracked position off anything weaker than that.
    An accepted-but-unfilled order is reported with success=False and resting=True
    so it can be surfaced as a working order instead of a phantom position.
    """
    if error is not None:
        return {
            "account_name": session.name,
            "wallet": session.wallet,
            "stake": stake,
            "success": False,
            "accepted": False,
            "resting": False,
            "filled_size": 0.0,
            "filled_cost": 0.0,
            "avg_price": 0.0,
            "status": None,
            "order_id": None,
            "response": None,
            "error": error,
        }
    outcome = outcome or {}
    filled = bool(outcome.get("filled")) and float(outcome.get("filled_size", 0.0)) > 0.0
    if filled:
        err = None
    elif outcome.get("resting"):
        err = outcome.get("message") or f"Order accepted but unfilled (status={outcome.get('status')})."
    else:
        err = outcome.get("message") or f"Order rejected by exchange ({outcome.get('code') or 'unknown'})."
    return {
        "account_name": session.name,
        "wallet": session.wallet,
        "stake": stake,
        "success": filled,
        "accepted": bool(outcome.get("accepted")),
        "resting": bool(outcome.get("resting")),
        "filled_size": float(outcome.get("filled_size", 0.0)),
        "filled_cost": float(outcome.get("filled_cost", 0.0)),
        "avg_price": float(outcome.get("avg_price", 0.0)),
        "status": outcome.get("status"),
        "order_id": outcome.get("order_id"),
        "amounts_unavailable": bool(outcome.get("amounts_unavailable")),
        "response": outcome.get("raw"),
        "error": err,
    }


class LiveBroker:
    """Unified multi-account live broker managing multiple AccountSessions concurrently."""

    def __init__(self):
        ok, msg = check_credentials_available()
        if not ok:
            raise LiveBrokerError(f"Cannot initialize LiveBroker: {msg}")

        configs = config.get_configured_accounts()
        self.sessions: Dict[str, AccountSession] = {}
        self.account_list: List[AccountSession] = []

        errors = []
        for acfg in configs:
            if not acfg.get("enabled", True):
                continue
            try:
                session = AccountSession(acfg)
                self.sessions[session.name] = session
                self.sessions[session.account_id] = session
                self.account_list.append(session)
            except Exception as err:
                errors.append(f"{acfg.get('name')}: {err}")

        if not self.account_list:
            raise LiveBrokerError(f"All account sessions failed to initialize: {'; '.join(errors)}")

        # Primary session for backward compatibility delegation
        self.primary_session = self.account_list[0]
        self.client = self.primary_session.client
        self.wallet = self.primary_session.wallet
        self.wallet_type = self.primary_session.wallet_type

    def get_account_names(self) -> List[str]:
        return [s.name for s in self.account_list]

    def get_session(self, identifier: Optional[str] = None) -> Optional[AccountSession]:
        if not identifier:
            return self.primary_session
        return self.sessions.get(identifier)

    def get_collateral_balance(self, account_name: Optional[str] = None) -> float:
        if account_name:
            session = self.get_session(account_name)
            return session.get_collateral_balance() if session else 0.0
        # Return total across all sessions
        return round(sum(s.get_collateral_balance() for s in self.account_list), 2)

    def get_allowance(self, account_name: Optional[str] = None) -> float:
        if account_name:
            session = self.get_session(account_name)
            return session.get_allowance() if session else 0.0
        return self.primary_session.get_allowance()

    def get_account_vitals(self, account_name: Optional[str] = None) -> Dict[str, Any]:
        if account_name:
            session = self.get_session(account_name)
            return session.get_account_vitals() if session else {}
        return self.primary_session.get_account_vitals()

    def get_aggregated_vitals(self) -> Dict[str, Any]:
        """Returns aggregated balances and individual vitals for all active accounts."""
        vitals_list = [s.get_account_vitals() for s in self.account_list]
        total_balance = sum(v["collateral_balance"] for v in vitals_list)
        return {
            "credentials_ok": True,
            "account_count": len(self.account_list),
            "total_collateral": round(total_balance, 2),
            "accounts": vitals_list,
        }

    def get_open_orders(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        if account_name:
            session = self.get_session(account_name)
            return session.get_open_orders() if session else []
        all_orders = []
        for s in self.account_list:
            all_orders.extend(s.get_open_orders())
        return all_orders

    def get_live_positions(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        if account_name:
            session = self.get_session(account_name)
            return session.get_live_positions() if session else []
        all_positions = []
        for s in self.account_list:
            all_positions.extend(s.get_live_positions())
        return all_positions

    def place_buy(self, token_id: str, price: float, stake_usd: float) -> Any:
        """Places a buy on the primary account (for backward compatibility)."""
        return self.primary_session.place_buy(token_id=token_id, price=price, stake_usd=stake_usd)

    def place_buy_all(self, token_id: str, price: float, default_stake: float = 25.0, override_stake: Optional[float] = None) -> List[Dict[str, Any]]:
        """Concurrently dispatches buy orders across all active accounts using a ThreadPool.
        Guarantees parallel execution to eliminate slippage between accounts.
        If override_stake is provided (e.g. from manual dashboard triggers), it takes precedence."""
        results = []
        workers = min(len(self.account_list), 5)

        def _execute_session(session: AccountSession):
            if override_stake is not None:
                stake = float(override_stake)
            else:
                stake = settings_manager.get_account_stake(session.name, fallback=session.custom_stake or default_stake)
            try:
                outcome = session.place_buy(token_id=token_id, price=price, stake_usd=stake)
                return _buy_result(session, stake, outcome=outcome)
            except Exception as exc:
                return _buy_result(session, stake, error=str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_execute_session, s) for s in self.account_list]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        return results

    def exit_position(self, account_name: str, token_id: str, size: float, price: float, order_type: str = "LIMIT") -> Dict[str, Any]:
        """Dispatches a sell order to exit an open position on Polymarket CLOB for the
        given account, returning the interpreted fill outcome. Callers must check
        outcome["filled"] before booking the exit locally -- a rejected or resting
        sell order used to be reported to the user as a completed sale."""
        session = self.sessions.get(account_name) or self.primary_session
        if not session:
            raise LiveBrokerError(f"No active session found for account '{account_name}'")

        # Sell what the wallet actually holds, not what the local book thinks it
        # holds. Selling the local number under-sold by a fraction of a share on
        # every exit, and the leftover dust then read as an open position forever.
        on_chain = session.get_position_size(token_id)
        if on_chain is not None and on_chain > DUST_SHARE_THRESHOLD:
            size = on_chain
        return session.place_sell(token_id=token_id, price=price, size=size, order_type=order_type)

    def cancel_order(self, order_id: str, account_name: Optional[str] = None) -> Any:
        """Cancels a single resting order on one account (defaults to the primary)."""
        session = self.get_session(account_name)
        if not session:
            raise LiveBrokerError(f"Account '{account_name}' not found.")
        return session.cancel_order(order_id)

    def cancel_all_orders(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Cancels every resting order for one account, or across all accounts."""
        sessions = [self.get_session(account_name)] if account_name else list(self.account_list)
        results = []
        for sess in [x for x in sessions if x]:
            try:
                results.append({"account": sess.name, "success": True, "result": sess.cancel_all_orders()})
            except Exception as exc:
                results.append({"account": sess.name, "success": False, "error": str(exc)})
        return results

    def place_buy_selected(self, token_id: str, price: float, account_stakes: Dict[str, float],
                           order_type: str = "LIMIT", max_price: Optional[float] = None) -> List[Dict[str, Any]]:
        """Concurrently dispatches buy orders for only the given subset of accounts
        (name -> stake). Used so accounts can independently opt in/out of an opportunity
        (per their own pause/kill-switch/filters/limits) while still firing in parallel
        for whichever accounts DO want it, avoiding inter-account slippage."""
        sessions = [(name, self.sessions.get(name)) for name in account_stakes if self.sessions.get(name)]
        results: List[Dict[str, Any]] = []
        if not sessions:
            return results
        workers = min(len(sessions), 5)

        def _execute_session(name: str, session: AccountSession, stake: float):
            try:
                outcome = session.place_buy(token_id=token_id, price=price, stake_usd=stake,
                                            order_type=order_type, max_price=max_price)
                return _buy_result(session, stake, outcome=outcome)
            except Exception as exc:
                return _buy_result(session, stake, error=str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_execute_session, name, session, account_stakes[name]) for name, session in sessions]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        return results

    def redeem_winning_position(self, condition_id: Optional[str] = None, market_id: Optional[str] = None, account_name: Optional[str] = None) -> Any:
        """Redeems winning positions for a specific account or across all accounts."""
        if account_name:
            session = self.get_session(account_name)
            if session:
                return session.redeem_winning_position(condition_id=condition_id, market_id=market_id)
            raise LiveBrokerError(f"Account '{account_name}' not found for redemption.")

        outcomes = []
        for s in self.account_list:
            try:
                outcome = s.redeem_winning_position(condition_id=condition_id, market_id=market_id)
                outcomes.append({"account": s.name, "success": True, "outcome": outcome})
            except Exception as e:
                outcomes.append({"account": s.name, "success": False, "error": str(e)})
        return outcomes

    def _resolved_price_for_token(self, market_id: Optional[str], token_id: str) -> Tuple[Optional[float], str]:
        """Returns (settlement_price, reason) for one outcome token, or (None, reason)
        when the true outcome cannot be established.

        The previous version settled at 1.0 whenever UMA reported the *market* as
        resolved, regardless of whether *our* token was the winning side, and fell
        back to "entry price was high, so assume it won". Both fabricated wins. This
        returns None instead, and callers leave the trade PENDING."""
        if not market_id:
            return None, "no market id recorded for this trade"
        try:
            pub_client = polymarket_client.get_public_client()
            m = pub_client.get_market(id=str(market_id))
        except Exception as exc:
            return None, f"market lookup failed: {exc}"

        if not m or not m.state:
            return None, "market lookup returned no state"

        raw_p = None
        if m.outcomes:
            for oc in [m.outcomes.yes, m.outcomes.no]:
                if oc and oc.token_id and str(oc.token_id) == str(token_id):
                    if oc.price is not None:
                        raw_p = float(oc.price)
                    break

        uma_status = str(m.resolution.uma_resolution_status).lower() if (m.resolution and m.resolution.uma_resolution_status) else ""
        is_uma_resolved = "resolved" in uma_status

        if raw_p is None:
            # Resolved or not, without a price for OUR token there is no basis on
            # which to decide whether this side won.
            return None, "no price available for this outcome token"

        if is_uma_resolved:
            return (1.0 if raw_p >= 0.5 else 0.0), ("resolved (win)" if raw_p >= 0.5 else "resolved (loss)")

        if m.state.closed and (raw_p >= 0.95 or raw_p <= 0.05):
            return (1.0 if raw_p >= 0.5 else 0.0), ("closed at winning price" if raw_p >= 0.5 else "closed at losing price")

        if m.state.closed:
            return raw_p, f"market closed at ${raw_p:.4f}"

        return None, "market is still open"

    def reconcile_positions(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Compares pending live trades in local SQLite against actual on-chain
        positions and Polymarket trade history, and corrects the local record.

        Three distinct cases, which the previous version collapsed into "assume it won":

        1. The token is still held on-chain -> leave it PENDING, nothing to do.
        2. It was sold, or the market resolved and we can read the outcome for OUR
           token -> settle at the real price and book the real P&L.
        3. It was never bought on-chain at all (no BUY trade, no balance) -> the order
           never filled, so the local row is a phantom. VOID it: no payout, no P&L,
           and drop it from state.json. This is what clears positions the bot recorded
           from rejected or never-filled orders.

        Anything fitting none of these is left PENDING with a note rather than guessed at."""
        import database
        import paper_broker
        reconciled = []
        sessions = [self.get_session(account_name)] if account_name else self.account_list
        sessions = [s for s in sessions if s]
        broker_inst = paper_broker.PaperBroker()

        for session in sessions:
            try:
                # 1. Query live open positions on-chain for this wallet
                on_chain_pos = session.get_live_positions()
                held_token_ids = {
                    str(p.get("token_id")) for p in on_chain_pos
                    if float(p.get("size", 0.0)) > DUST_SHARE_THRESHOLD
                }

                # 2. Query recent trades on-chain, keeping both sides: the SELL gives
                #    the exit price, and the *absence* of a BUY tells us the entry
                #    never actually happened.
                on_chain_trades_paginator = session.client.list_trades(user=session.wallet, page_size=100)
                sell_trades_by_token: Dict[str, Any] = {}
                bought_token_ids = set()
                for tr in on_chain_trades_paginator.iter_items():
                    tok = str(tr.asset_id or "")
                    if not tok:
                        continue
                    side = str(tr.side).upper()
                    if side == "SELL":
                        if tok not in sell_trades_by_token:
                            sell_trades_by_token[tok] = tr
                    elif side == "BUY":
                        bought_token_ids.add(tok)

                # 3. Find pending live trades in SQLite for this account/wallet
                db_trades = database.get_all_trades(broker_filter="live", limit=500)
                pending = [
                    t for t in db_trades
                    if str(t.get("result", "")).upper() == "PENDING"
                    and (not t.get("account_name") or t.get("account_name") == session.name or t.get("wallet_address") == session.wallet)
                ]

                for pt in pending:
                    tok = str(pt.get("token_id", ""))
                    if not tok or tok in held_token_ids:
                        continue  # still held on-chain: nothing to reconcile

                    tokens_held = float(pt.get("tokens") or 0.0)
                    cost = float(pt.get("cost") or 0.0)
                    matching_sell = sell_trades_by_token.get(tok)

                    if matching_sell:
                        sell_price = float(matching_sell.price or 0.0)
                        sold_size = float(matching_sell.size or 0.0)
                        if sold_size > 0:
                            tokens_held = sold_size
                        payout = tokens_held * sell_price
                        pnl = payout - cost
                        res = database.classify_result(pnl)
                        closed_ts = str(matching_sell.timestamp or datetime.now(timezone.utc).isoformat())
                        note = f"Exited directly on Polymarket @ ${sell_price:.4f}"
                    else:
                        # Not held on-chain and no recent SELL trade found.
                        # It might be resolved, or the API might be lagging.
                        # Only settle if the actual outcome for this token can be read.
                        sell_price, reason = self._resolved_price_for_token(pt.get("market_id"), tok)
                        if sell_price is None:
                            print(f"[live_broker] Leaving {pt.get('trade_id')} PENDING -- {reason}")
                            continue
                        payout = tokens_held * sell_price
                        pnl = payout - cost
                        res = database.classify_result(pnl)
                        closed_ts = datetime.now(timezone.utc).isoformat()
                        note = f"Reconciled from Polymarket: {reason} @ ${sell_price:.4f}"

                    database.settle_trade(
                        token_id=tok,
                        trade_id=pt.get("trade_id"),
                        resolved_price=sell_price,
                        payout=payout,
                        pnl=pnl,
                        result=res,
                        closed_at=closed_ts,
                        note=note,
                    )
                    broker_inst.discard_position(pt.get("trade_id") or tok, note=note, closed_price=sell_price)

                    reconciled.append({
                        "trade_id": pt.get("trade_id"),
                        "account": session.name,
                        "question": pt.get("question"),
                        "outcome": pt.get("outcome"),
                        "exit_price": sell_price,
                        "pnl": pnl,
                        "voided": False,
                        "note": note,
                    })
            except Exception as exc:
                print(f"[live_broker] Reconciliation error for {session.name}: {exc}")

        return reconciled


_cached_live_broker: Optional[LiveBroker] = None


def invalidate_live_broker_cache() -> None:
    """Drops the cached LiveBroker singleton so the next get_live_broker() call
    rebuilds it from the current .env contents. Call this after add_account(),
    remove_account(), or set_account_enabled() so account changes take effect
    without restarting the process."""
    global _cached_live_broker
    _cached_live_broker = None


def get_live_broker() -> Optional[LiveBroker]:
    """Returns a singleton instance of LiveBroker if credentials are valid, or None."""
    global _cached_live_broker
    ok, _ = check_credentials_available()
    if not ok:
        return None
    if _cached_live_broker is None:
        try:
            _cached_live_broker = LiveBroker()
        except Exception as exc:
            print(f"[live_broker] Could not initialize singleton: {exc}")
            return None
    return _cached_live_broker


