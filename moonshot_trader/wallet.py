"""Solana wallet management — load keypair, check balances, sign and send transactions."""

import base64
import json
import logging
from typing import Optional

import base58
from solana.rpc.api import Client
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import moonshot_trader.config as cfg

logger = logging.getLogger(__name__)

_TOKEN_PROGRAM_ID = Pubkey.from_string(cfg.TOKEN_PROGRAM_ID)


def load_keypair(private_key_str: str) -> Keypair:
    """
    Load keypair from:
      - base58-encoded 64-byte secret key (Phantom "Export Private Key")
      - JSON byte array string like "[12,34,...]" (Solana CLI id.json)
    """
    s = private_key_str.strip()
    if s.startswith("["):
        key_bytes = bytes(json.loads(s))
    else:
        key_bytes = base58.b58decode(s)
    return Keypair.from_bytes(key_bytes)


def make_client(rpc_endpoint: str) -> Client:
    return Client(rpc_endpoint)


def get_sol_balance(client: Client, pubkey: Pubkey) -> float:
    result = client.get_balance(pubkey)
    return result.value / 1e9


def get_token_balances(client: Client, pubkey: Pubkey) -> dict[str, dict]:
    """
    Returns {mint_address: {"ui_amount": float, "decimals": int}}
    for all SPL tokens with a non-zero balance.
    """
    try:
        result = client.get_token_accounts_by_owner_json_parsed(
            pubkey,
            TokenAccountOpts(program_id=_TOKEN_PROGRAM_ID),
        )
    except Exception as e:
        logger.warning("Failed to fetch token accounts: %s", e)
        return {}

    balances: dict[str, dict] = {}
    for account in result.value:
        try:
            info = account.account.data.parsed["info"]
            mint = info["mint"]
            token_amount = info["tokenAmount"]
            ui_amount = float(token_amount.get("uiAmount") or 0)
            decimals = int(token_amount.get("decimals", 6))
            if ui_amount > 0:
                balances[mint] = {"ui_amount": ui_amount, "decimals": decimals}
        except (KeyError, TypeError):
            continue

    return balances


def sign_and_send(
    client: Client,
    swap_tx_b64: str,
    keypair: Keypair,
) -> Optional[str]:
    """
    Deserialize a Jupiter base64 versioned transaction, sign it, broadcast it.
    Returns the transaction signature string, or None on failure.
    """
    try:
        raw_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)

        # Sign the serialized message
        signature = keypair.sign_message(bytes(tx.message))
        signed_tx = VersionedTransaction(tx.message, [signature])

        result = client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(
                skip_preflight=True,
                preflight_commitment="confirmed",
                max_retries=3,
            ),
        )
        sig = str(result.value)
        logger.info("Transaction sent: %s", sig)
        return sig

    except Exception as e:
        logger.error("sign_and_send failed: %s", e)
        return None


def confirm_transaction(client: Client, signature: str, timeout_seconds: int = 45) -> bool:
    """Poll until transaction is confirmed or timeout."""
    import time
    from solders.signature import Signature

    sig = Signature.from_string(signature)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = client.get_signature_statuses([sig])
            status = resp.value[0]
            if status is not None:
                if status.err:
                    logger.warning("Transaction %s failed on-chain: %s", signature, status.err)
                    return False
                if status.confirmation_status in ("confirmed", "finalized"):
                    return True
        except Exception as e:
            logger.debug("Status check error: %s", e)
        time.sleep(2)

    logger.warning("Transaction %s timed out waiting for confirmation", signature)
    return False
