#!/usr/bin/env python3
"""
PolyBMiCoB - Claim Winnings Script

Standalone script to detect and claim all resolved Polymarket winnings.
Can be run manually or via cron.

Usage:
  python scripts/claim_winnings.py              # dry-run (detect only)
  python scripts/claim_winnings.py --live       # actually claim on-chain
  python scripts/claim_winnings.py --check      # just check, no claiming

How it works:
  1. Queries Polymarket Data API for positions with redeemable=true
  2. For each redeemable position, encodes redeemPositions() calldata
  3. Submits via Polymarket Builder Relayer (gasless, no MATIC needed)
  4. Winning tokens are burned and USDC.e is returned to your wallet
"""

import logging
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from lib.claim_winnings import claim_all_winnings, get_claimable_positions

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(DATA_DIR / "claim_winnings.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("polybmicob.claim")

# ──────────────────────────────────────────────────────────────
# Config from .env
# ──────────────────────────────────────────────────────────────

PRIVATE_KEY = os.environ.get("POLYBMICOB_PRIVATE_KEY", "")
PROXY_WALLET = os.environ.get("POLYMARKET_PROXY_WALLET", "")
BUILDER_KEY = os.environ.get("POLY_BUILDER_API_KEY", "")
BUILDER_SECRET = os.environ.get("POLY_BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.environ.get("POLY_BUILDER_PASSPHRASE", "")


def main() -> None:
    """Main entry point."""
    if not PROXY_WALLET:
        log.error("POLYMARKET_PROXY_WALLET not set in .env")
        sys.exit(1)

    # Parse args
    check_only = "--check" in sys.argv
    live = "--live" in sys.argv
    dry_run = not live

    if check_only:
        # Just list claimable positions, no transactions
        log.info("=" * 50)
        log.info("CLAIM CHECK - listing redeemable positions")
        log.info("=" * 50)

        positions = get_claimable_positions(PROXY_WALLET)

        if not positions:
            log.info("No claimable positions found.")
            return

        total = sum(p.current_value for p in positions)
        log.info("Found %d claimable position(s):", len(positions))
        for p in positions:
            log.info(
                "  $%.2f  %s [%s]  neg_risk=%s  condition=%s...",
                p.current_value,
                p.title,
                p.outcome,
                p.negative_risk,
                p.condition_id[:18],
            )
        log.info("Total claimable: $%.2f", total)
        return

    if dry_run:
        log.info("=" * 50)
        log.info("CLAIM DRY RUN (use --live to actually claim)")
        log.info("=" * 50)
    else:
        if not PRIVATE_KEY:
            log.error("POLYBMICOB_PRIVATE_KEY not set in .env")
            sys.exit(1)
        if not BUILDER_KEY:
            log.error("POLY_BUILDER_API_KEY not set in .env (register at builders.polymarket.com)")
            sys.exit(1)
        log.info("=" * 50)
        log.info("CLAIM LIVE - gasless via Polymarket relayer")
        log.info("=" * 50)

    results = claim_all_winnings(
        proxy_wallet=PROXY_WALLET,
        private_key=PRIVATE_KEY,
        builder_key=BUILDER_KEY,
        builder_secret=BUILDER_SECRET,
        builder_passphrase=BUILDER_PASSPHRASE,
        dry_run=dry_run,
    )

    if results:
        succeeded = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)
        log.info("Summary: %d claimed, %d failed", succeeded, failed)
        for r in results:
            status = "OK" if r.success else f"FAIL: {r.error}"
            log.info("  %s - %s (tx: %s)", r.title, status, r.tx_hash[:20] if r.tx_hash else "n/a")


if __name__ == "__main__":
    main()
