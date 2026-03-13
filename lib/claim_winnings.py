"""
Auto-claim resolved Polymarket winnings via gasless relayer.

Flow:
  1. Query Data API for redeemable positions (user's proxy wallet)
  2. For each redeemable position, encode redeemPositions() calldata
  3. Submit via Polymarket Builder Relayer (gasless, no MATIC needed)

Polymarket uses two contract types:
  - Standard markets (negativeRisk=false): ConditionalTokens contract
  - NegRisk markets (negativeRisk=true): NegRiskAdapter contract

BTC 5m Up/Down markets are standard (not negRisk).

Contract addresses (Polygon mainnet):
  - ConditionalTokens: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  - NegRiskAdapter:    0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
  - USDC.e:            0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import logging
from dataclasses import dataclass

import httpx
from eth_abi import encode
from eth_utils import keccak

from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import RelayerTxType, Transaction
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

log = logging.getLogger("polybmicob.claim")

# ──────────────────────────────────────────────────────────────
# Contract addresses (Polygon mainnet)
# ──────────────────────────────────────────────────────────────

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

RELAYER_URL = "https://relayer-v2.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

ZERO_BYTES32 = b"\x00" * 32


# ──────────────────────────────────────────────────────────────
# ABI encoding helpers
# ──────────────────────────────────────────────────────────────


def _function_selector(signature: str) -> bytes:
    """First 4 bytes of Keccak-256 of the function signature."""
    return keccak(text=signature)[:4]


def encode_redeem_standard(condition_id: str) -> str:
    """
    Encode redeemPositions(address, bytes32, bytes32, uint256[]) calldata.

    For standard (non-negRisk) markets:
      redeemPositions(USDC, bytes32(0), conditionId, [1, 2])
    """
    selector = _function_selector(
        "redeemPositions(address,bytes32,bytes32,uint256[])"
    )

    # Convert condition_id hex string to bytes32
    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))

    encoded_args = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_ADDRESS, ZERO_BYTES32, cid_bytes, [1, 2]],
    )

    return "0x" + (selector + encoded_args).hex()


def encode_redeem_neg_risk(condition_id: str, amount: int) -> str:
    """
    Encode redeemPositions(bytes32, uint256[]) calldata.

    For negRisk markets:
      redeemPositions(conditionId, [amount, amount])
    """
    selector = _function_selector("redeemPositions(bytes32,uint256[])")

    cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))

    encoded_args = encode(
        ["bytes32", "uint256[]"],
        [cid_bytes, [amount, amount]],
    )

    return "0x" + (selector + encoded_args).hex()


# ──────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────


@dataclass
class ClaimablePosition:
    """A position that can be redeemed for USDC."""

    condition_id: str
    title: str
    outcome: str
    size: float
    current_value: float
    negative_risk: bool
    slug: str


@dataclass
class ClaimResult:
    """Result of a single claim transaction."""

    condition_id: str
    title: str
    tx_hash: str
    success: bool
    error: str = ""


# ──────────────────────────────────────────────────────────────
# Step 1: Detect claimable positions via Data API
# ──────────────────────────────────────────────────────────────


def get_claimable_positions(proxy_wallet: str) -> list[ClaimablePosition]:
    """
    Query Polymarket Data API for redeemable positions.

    Args:
        proxy_wallet: The user's Polymarket proxy wallet address.

    Returns:
        List of ClaimablePosition objects.
    """
    positions = []
    offset = 0
    limit = 100

    while True:
        try:
            resp = httpx.get(
                f"{DATA_API_BASE}/positions",
                params={
                    "user": proxy_wallet,
                    "redeemable": "true",
                    "limit": limit,
                    "offset": offset,
                    "sizeThreshold": 0,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("Failed to fetch claimable positions: %s", exc)
            break

        if not data:
            break

        for pos in data:
            condition_id = pos.get("conditionId", "")
            if not condition_id:
                continue

            positions.append(
                ClaimablePosition(
                    condition_id=condition_id,
                    title=pos.get("title", "Unknown"),
                    outcome=pos.get("outcome", "?"),
                    size=float(pos.get("size", 0)),
                    current_value=float(pos.get("currentValue", 0)),
                    negative_risk=bool(pos.get("negativeRisk", False)),
                    slug=pos.get("slug", ""),
                )
            )

        if len(data) < limit:
            break
        offset += limit

    # Deduplicate by condition_id (API may return both YES and NO positions)
    seen = set()
    unique = []
    for p in positions:
        if p.condition_id not in seen:
            seen.add(p.condition_id)
            unique.append(p)

    return unique


# ──────────────────────────────────────────────────────────────
# Step 2: Redeem via gasless relayer
# ──────────────────────────────────────────────────────────────


def _init_relay_client(
    private_key: str,
    builder_key: str,
    builder_secret: str,
    builder_passphrase: str,
) -> RelayClient:
    """Initialize the Polymarket Builder Relayer client."""
    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_passphrase,
        )
    )

    client = RelayClient(
        RELAYER_URL,
        137,  # Polygon mainnet
        private_key,
        builder_config,
        RelayerTxType.PROXY,
    )

    return client


def redeem_via_relayer(
    client: RelayClient,
    position: ClaimablePosition,
) -> ClaimResult:
    """
    Redeem a single position via gasless relayer.

    Encodes the redeemPositions calldata and submits via relayer.
    """
    try:
        if position.negative_risk:
            amount = int(position.size * 1_000_000)
            calldata = encode_redeem_neg_risk(position.condition_id, amount)
            target = NEG_RISK_ADAPTER_ADDRESS
        else:
            calldata = encode_redeem_standard(position.condition_id)
            target = CTF_ADDRESS

        txn = Transaction(
            to=target,
            data=calldata,
            value="0",
        )

        log.info("  Submitting gasless redeem to relayer...")
        resp = client.execute([txn], f"Redeem {position.title}")

        # Wait for transaction to be mined
        result = resp.wait()

        if result is not None:
            tx_hash = result.get("transactionHash", "")
            state = result.get("state", "")
            if state == "STATE_CONFIRMED" or state == "STATE_MINED":
                return ClaimResult(
                    condition_id=position.condition_id,
                    title=position.title,
                    tx_hash=tx_hash,
                    success=True,
                )
            else:
                return ClaimResult(
                    condition_id=position.condition_id,
                    title=position.title,
                    tx_hash=tx_hash,
                    success=False,
                    error=f"Unexpected state: {state}",
                )
        else:
            # resp object has transaction_id and transaction_hash
            tx_hash = getattr(resp, "transaction_hash", "") or ""
            tx_id = getattr(resp, "transaction_id", "") or ""
            return ClaimResult(
                condition_id=position.condition_id,
                title=position.title,
                tx_hash=tx_hash or tx_id,
                success=False,
                error="Transaction timed out or failed",
            )

    except Exception as exc:
        return ClaimResult(
            condition_id=position.condition_id,
            title=position.title,
            tx_hash="",
            success=False,
            error=str(exc),
        )


# ──────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────


def claim_all_winnings(
    proxy_wallet: str,
    private_key: str,
    builder_key: str = "",
    builder_secret: str = "",
    builder_passphrase: str = "",
    dry_run: bool = False,
    **kwargs,
) -> list[ClaimResult]:
    """
    Detect and claim all redeemable positions via gasless relayer.

    Args:
        proxy_wallet: Polymarket proxy wallet address.
        private_key: Private key for signing relay transactions.
        builder_key: Builder API key.
        builder_secret: Builder API secret.
        builder_passphrase: Builder API passphrase.
        dry_run: If True, only detect but don't execute transactions.

    Returns:
        List of ClaimResult objects.
    """
    log.info("Checking for claimable positions (wallet: %s)...", proxy_wallet)

    positions = get_claimable_positions(proxy_wallet)

    if not positions:
        log.info("No claimable positions found.")
        return []

    total_value = sum(p.current_value for p in positions)
    log.info(
        "Found %d claimable position(s) worth ~$%.2f",
        len(positions),
        total_value,
    )

    for p in positions:
        log.info(
            "  - %s [%s] size=%.2f value=$%.2f neg_risk=%s",
            p.title,
            p.outcome,
            p.size,
            p.current_value,
            p.negative_risk,
        )

    if dry_run:
        log.info("DRY RUN: skipping gasless redemption.")
        return [
            ClaimResult(
                condition_id=p.condition_id,
                title=p.title,
                tx_hash="dry-run",
                success=True,
            )
            for p in positions
        ]

    # Check builder credentials
    if not builder_key or not builder_secret or not builder_passphrase:
        log.error(
            "Builder API credentials required for gasless claiming. "
            "Set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, "
            "POLY_BUILDER_PASSPHRASE in .env"
        )
        return []

    # Initialize relayer client
    client = _init_relay_client(
        private_key, builder_key, builder_secret, builder_passphrase
    )
    log.info("Relayer client initialized, submitting gasless claims...")

    results = []
    for p in positions:
        log.info(
            "Claiming: %s [%s] (conditionId: %s...)",
            p.title,
            p.outcome,
            p.condition_id[:18],
        )

        result = redeem_via_relayer(client, p)
        results.append(result)

        if result.success:
            log.info("  Claimed OK: %s", result.tx_hash)
        else:
            log.warning("  Claim FAILED: %s", result.error)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    log.info("Claim complete: %d succeeded, %d failed", succeeded, failed)

    return results
