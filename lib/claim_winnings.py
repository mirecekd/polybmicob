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

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
    failure_type: str = ""       # "quota_exceeded", "relayer_error", "tx_failed"
    retry_after_sec: int = 0     # seconds until relayer quota resets
    queued: bool = False         # True if enqueued for external agent


# ──────────────────────────────────────────────────────────────
# Relayer quota detection
# ──────────────────────────────────────────────────────────────

# Module-level cooldown: skip relayer calls until this epoch time
_relayer_cooldown_until: float = 0.0


def _is_quota_exceeded(error_str: str) -> tuple[bool, int]:
    """
    Check if an error string indicates relayer quota exhaustion.

    Returns:
        (is_quota_error, retry_after_seconds)
    """
    if "status_code=429" not in error_str and "quota exceeded" not in error_str:
        return False, 0

    # Try to parse "resets in N seconds"
    match = re.search(r"resets in (\d+) seconds", error_str)
    retry_after = int(match.group(1)) if match else 3600  # default 1h if unparseable

    return True, retry_after


def is_relayer_in_cooldown() -> bool:
    """Check if relayer is currently in quota cooldown."""
    return time.time() < _relayer_cooldown_until


def get_relayer_cooldown_remaining() -> int:
    """Seconds remaining in relayer cooldown (0 if not in cooldown)."""
    remaining = _relayer_cooldown_until - time.time()
    return max(0, int(remaining))


# ──────────────────────────────────────────────────────────────
# Claim queue (fallback for external agent)
# ──────────────────────────────────────────────────────────────

CLAIM_QUEUE_FILE = Path(__file__).parent.parent / "data" / "claim_queue.json"


def load_claim_queue() -> list[dict]:
    """Load pending claim queue from disk."""
    if not CLAIM_QUEUE_FILE.exists():
        return []
    try:
        return json.loads(CLAIM_QUEUE_FILE.read_text())
    except Exception:
        return []


def save_claim_queue(items: list[dict]) -> None:
    """Save claim queue to disk."""
    CLAIM_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLAIM_QUEUE_FILE.write_text(json.dumps(items, indent=2))


def enqueue_claim(position: ClaimablePosition, reason: str) -> bool:
    """
    Add a position to the claim fallback queue. Deduplicates by condition_id.

    Returns True if newly enqueued, False if already present.
    """
    queue = load_claim_queue()

    # Dedupe: skip if already pending for this condition_id
    existing_ids = {
        item["condition_id"]
        for item in queue
        if item.get("status") == "pending"
    }
    if position.condition_id in existing_ids:
        log.info(
            "  Claim already queued, skipping duplicate: %s",
            position.condition_id[:18],
        )
        return False

    queue.append({
        "condition_id": position.condition_id,
        "title": position.title,
        "outcome": position.outcome,
        "slug": position.slug,
        "negative_risk": position.negative_risk,
        "size": position.size,
        "current_value": position.current_value,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "status": "pending",
    })

    save_claim_queue(queue)
    log.info(
        "  Queued claim fallback: %s [%s] conditionId=%s",
        position.title,
        position.outcome,
        position.condition_id[:18],
    )
    return True


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
    global _relayer_cooldown_until

    # Gate: skip entirely if relayer is in quota cooldown
    if is_relayer_in_cooldown():
        remaining = get_relayer_cooldown_remaining()
        log.info(
            "Relayer in quota cooldown (%d min %d sec remaining), skipping claim cycle.",
            remaining // 60,
            remaining % 60,
        )
        return []

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
    for idx, p in enumerate(positions):
        log.info(
            "Claiming: %s [%s] (conditionId: %s...)",
            p.title,
            p.outcome,
            p.condition_id[:18],
        )

        result = redeem_via_relayer(client, p)

        # Check for relayer quota exhaustion (429)
        is_quota, retry_sec = _is_quota_exceeded(result.error)
        if is_quota:
            result.failure_type = "quota_exceeded"
            result.retry_after_sec = retry_sec

            # Set module-level cooldown
            _relayer_cooldown_until = time.time() + retry_sec
            log.warning(
                "Relayer quota exceeded! Resets in %d min %d sec. "
                "Entering cooldown, queueing remaining claims.",
                retry_sec // 60,
                retry_sec % 60,
            )

            # Enqueue this failed position
            enqueue_claim(p, "relayer_quota_exceeded")
            result.queued = True
            results.append(result)

            # Enqueue all remaining positions (don't even try relayer)
            remaining = positions[idx + 1:]
            if remaining:
                log.info(
                    "Queueing %d remaining claim(s) for external agent...",
                    len(remaining),
                )
            for rp in remaining:
                enqueue_claim(rp, "relayer_quota_exceeded")
                results.append(ClaimResult(
                    condition_id=rp.condition_id,
                    title=rp.title,
                    tx_hash="",
                    success=False,
                    error="Queued (relayer quota exceeded)",
                    failure_type="quota_exceeded",
                    retry_after_sec=retry_sec,
                    queued=True,
                ))

            # Stop the claim loop - further relayer calls would all 429
            break

        results.append(result)

        if result.success:
            log.info("  Claimed OK: %s", result.tx_hash)
        else:
            log.warning("  Claim FAILED: %s", result.error)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    queued = sum(1 for r in results if r.queued)
    log.info(
        "Claim complete: %d succeeded, %d failed, %d queued for external agent",
        succeeded,
        failed,
        queued,
    )

    return results
