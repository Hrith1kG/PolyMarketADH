"""Polymarket client provider using the unified polymarket-client SDK."""
from typing import Optional
from polymarket import PublicClient, SecureClient, RelayerApiKey

_public_client: Optional[PublicClient] = None


def get_public_client() -> PublicClient:
    """Returns a shared singleton PublicClient for public reads."""
    global _public_client
    if _public_client is None:
        _public_client = PublicClient()
    return _public_client


def get_secure_client(
    private_key: str,
    wallet: Optional[str] = None,
    relayer_api_key: Optional[str] = None,
    relayer_api_key_address: Optional[str] = None,
) -> SecureClient:
    """Creates an authenticated SecureClient for trading, supporting both direct EOA
    and gasless Polymarket Relayer accounts."""
    api_key = None
    if relayer_api_key and relayer_api_key_address:
        api_key = RelayerApiKey(key=relayer_api_key, address=relayer_api_key_address)

    return SecureClient.create(
        private_key=private_key,
        wallet=wallet or None,
        api_key=api_key,
    )
