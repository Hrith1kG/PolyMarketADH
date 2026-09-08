"""Real order execution, account vitals tracking, and CTF position redemption
via Polymarket's official polymarket-client SDK across single or multiple accounts."""
import concurrent.futures
from typing import Dict, Any, List, Optional, Tuple
import config
import polymarket_client
import settings_manager


class LiveBrokerError(Exception):
    pass


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
            for order in paginator:
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
            for p in paginator:
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

    def place_buy(self, token_id: str, price: float, stake_usd: Optional[float] = None) -> Any:
        """Places a limit buy for stake_usd shares at `price`."""
        stake = stake_usd if stake_usd is not None else settings_manager.get_account_stake(self.name, fallback=self.custom_stake or config.STAKE_PER_TRADE)
        size = round(stake / price, 2)
        try:
            response = self.client.place_limit_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            return response
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to place order: {exc}")

    def redeem_winning_position(self, condition_id: Optional[str] = None, market_id: Optional[str] = None) -> Any:
        """Redeems resolved winning outcome tokens on CTF contract back to collateral."""
        try:
            tx = self.client.redeem_positions(condition_id=condition_id, market_id=market_id)
            outcome = tx.wait()
            return outcome
        except Exception as exc:
            raise LiveBrokerError(f"[{self.name}] Failed to redeem CTF position: {exc}")


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

    def place_buy_all(self, token_id: str, price: float, default_stake: float = 25.0) -> List[Dict[str, Any]]:
        """Concurrently dispatches buy orders across all active accounts using a ThreadPool.
        Guarantees parallel execution to eliminate slippage between accounts."""
        results = []
        workers = min(len(self.account_list), 5)

        def _execute_session(session: AccountSession):
            stake = settings_manager.get_account_stake(session.name, fallback=session.custom_stake or default_stake)
            try:
                resp = session.place_buy(token_id=token_id, price=price, stake_usd=stake)
                return {
                    "account_name": session.name,
                    "wallet": session.wallet,
                    "stake": stake,
                    "success": True,
                    "response": resp,
                    "error": None,
                }
            except Exception as exc:
                return {
                    "account_name": session.name,
                    "wallet": session.wallet,
                    "stake": stake,
                    "success": False,
                    "response": None,
                    "error": str(exc),
                }

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_execute_session, s) for s in self.account_list]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        return results

    def place_buy_selected(self, token_id: str, price: float, account_stakes: Dict[str, float]) -> List[Dict[str, Any]]:
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
                resp = session.place_buy(token_id=token_id, price=price, stake_usd=stake)
                return {
                    "account_name": name,
                    "wallet": session.wallet,
                    "stake": stake,
                    "success": True,
                    "response": resp,
                    "error": None,
                }
            except Exception as exc:
                return {
                    "account_name": name,
                    "wallet": session.wallet,
                    "stake": stake,
                    "success": False,
                    "response": None,
                    "error": str(exc),
                }

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


_cached_live_broker: Optional[LiveBroker] = None


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


