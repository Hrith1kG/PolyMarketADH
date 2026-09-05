"""Real order execution via Polymarket's official py-clob-client.

This module is NOT wired into main.py by default. It only activates if you set
LIVE_TRADING=true in .env AND type the confirmation phrase this module prompts for
at startup. Read README.md's "Going live" section first -- this places real orders
with real funds on Polygon mainnet and cannot be undone once filled.
"""
import config

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    ClobClient = None


class LiveBrokerError(Exception):
    pass


def require_confirmation():
    print("=" * 70)
    print("LIVE TRADING IS ENABLED. Real orders will be placed with real funds.")
    print(f"  Stake per trade : ${config.STAKE_PER_TRADE}")
    print(f"  Max open positions: {config.MAX_OPEN_POSITIONS}")
    print(f"  Max total exposure: ${config.MAX_TOTAL_EXPOSURE}")
    print("=" * 70)
    answer = input('Type "I UNDERSTAND THE RISK" to continue: ')
    if answer.strip() != "I UNDERSTAND THE RISK":
        raise LiveBrokerError("Confirmation phrase did not match. Aborting.")


class LiveBroker:
    def __init__(self):
        if ClobClient is None:
            raise LiveBrokerError(
                "py-clob-client is not installed. Run: pip install -r requirements-live.txt"
            )
        if not config.PRIVATE_KEY:
            raise LiveBrokerError("PRIVATE_KEY is not set in .env")

        require_confirmation()

        self.client = ClobClient(
            config.CLOB_BASE_URL,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS or None,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def place_buy(self, token_id, price, stake_usd):
        """Places a GTC limit buy for stake_usd worth of shares at `price`."""
        size = round(stake_usd / price, 2)
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.GTC)
