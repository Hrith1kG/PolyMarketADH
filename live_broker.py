"""Real order execution, account vitals tracking, and CTF position redemption
via Polymarket's official polymarket-client SDK."""
from typing import Dict, Any, List, Optional, Tuple
import config
import polymarket_client


class LiveBrokerError(Exception):
    pass


def check_credentials_available() -> Tuple[bool, str]:
    """Checks whether valid live trading credentials are set in .env or environment."""
    pk = config.PRIVATE_KEY
    if not pk or "your_private_key" in pk or len(pk.strip()) < 32:
        return False, "PRIVATE_KEY is missing or contains placeholder."
    return True, "Credentials OK"


class LiveBroker:
    def __init__(self):
        ok, msg = check_credentials_available()
        if not ok:
            raise LiveBrokerError(f"Cannot initialize LiveBroker: {msg}")

        try:
            self.client = polymarket_client.get_secure_client(
                private_key=config.PRIVATE_KEY,
                wallet=config.FUNDER_ADDRESS or None,
                relayer_api_key=config.RELAYER_API_KEY or None,
                relayer_api_key_address=config.RELAYER_API_KEY_ADDRESS or None,
            )
            self.wallet = str(self.client.wallet) if self.client.wallet else (config.FUNDER_ADDRESS or "Unknown")
            self.wallet_type = str(getattr(self.client, "wallet_type", "EOA"))
        except Exception as exc:
            raise LiveBrokerError(f"Failed to initialize SecureClient: {exc}")

    def get_collateral_balance(self) -> float:
        """Queries the live USDC.e collateral balance of the connected account."""
        try:
            ba = self.client.get_balance_allowance(asset_type="COLLATERAL")
            if ba and ba.balance is not None:
                # Raw units (USDC has 6 decimals on Polygon)
                return round(float(ba.balance) / 1_000_000.0, 2)
            return 0.0
        except Exception as exc:
            print(f"[live_broker] Failed to query balance: {exc}")
            return 0.0

    def get_allowance(self) -> float:
        """Queries the live USDC.e collateral allowance."""
        try:
            ba = self.client.get_balance_allowance(asset_type="COLLATERAL")
            if ba and ba.allowances:
                max_raw = max(ba.allowances.values()) if ba.allowances else 0
                return round(float(max_raw) / 1_000_000.0, 2)
            return 0.0
        except Exception as exc:
            print(f"[live_broker] Failed to query allowance: {exc}")
            return 0.0

    def get_account_vitals(self) -> Dict[str, Any]:
        """Returns complete live account status and balances."""
        return {
            "credentials_ok": True,
            "wallet": self.wallet,
            "wallet_type": self.wallet_type,
            "funder_address": config.FUNDER_ADDRESS or None,
            "collateral_balance": self.get_collateral_balance(),
            "allowance": self.get_allowance(),
        }

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Queries currently active limit orders on the CLOB."""
        try:
            paginator = self.client.list_open_orders()
            orders = []
            for order in paginator:
                orders.append({
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
            print(f"[live_broker] Failed to list open orders: {exc}")
            return []

    def get_live_positions(self) -> List[Dict[str, Any]]:
        """Queries on-chain positions for the account."""
        try:
            paginator = self.client.list_positions(user=self.wallet)
            positions = []
            for p in paginator:
                positions.append({
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
            print(f"[live_broker] Failed to list live positions: {exc}")
            return []

    def place_buy(self, token_id: str, price: float, stake_usd: float):
        """Places a limit buy for stake_usd worth of shares at `price`."""
        size = round(stake_usd / price, 2)
        try:
            response = self.client.place_limit_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            return response
        except Exception as exc:
            raise LiveBrokerError(f"Failed to place order: {exc}")

    def redeem_winning_position(self, condition_id: str = None, market_id: str = None):
        """Redeems resolved winning outcome tokens on the Conditional Tokens Framework (CTF)
        smart contract back into collateral (USDC/pUSD)."""
        try:
            tx = self.client.redeem_positions(condition_id=condition_id, market_id=market_id)
            outcome = tx.wait()
            return outcome
        except Exception as exc:
            raise LiveBrokerError(f"Failed to redeem on-chain CTF position: {exc}")


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

