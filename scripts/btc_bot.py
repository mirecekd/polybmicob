#!/usr/bin/env python3
"""
PolyBMiCoB - Polymarket BTC Micro-Cycle Options Bot

Main bot loop:
  1. Scan Gamma API for upcoming BTC 5-minute Up/Down markets
  2. Get BTC price momentum from Binance
  3. For each market: analyze orderbook + generate signal
  4. If signal has sufficient edge: place GTC limit order
  5. Log everything, sleep, repeat

Uses ClobClient directly (NOT ClobClientWrapper which has signature_type bug).
Hold-to-resolution strategy: no stop-loss/take-profit for 5-min markets.

Usage:
  cd /path/to/polybmicob
  workon polybmicob
  python scripts/btc_bot.py [--dry-run]
"""

import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from lib.btc_market_scanner import scan_btc_5m_markets
from lib.price_feed import get_btc_momentum, get_fear_greed_index
from lib.claim_winnings import claim_all_winnings
from lib.early_exit import ExitSignal, check_early_exits
from lib.flash_crash_detector import detect_flash_crashes, FlashCrashSignal
from lib.in_play_engine import analyze_in_play, scan_in_play_markets
from lib.ws_price_feed import BtcPriceFeed
from lib.resolution_tracker import resolve_trades
from lib.signal_engine import (
    OrderbookSignal,
    TradeSignal,
    compute_orderbook_imbalance,
    generate_signal,
    kelly_fraction,
    calculate_poly_fee_rate,
)
from lib.stats_collector import (
    record_cycle,
    record_momentum_skip,
    record_no_signal,
    record_pre_signal,
    record_inplay_signal,
    record_order_filled,
    record_order_rejected,
    record_resolution,
    record_wallet_balance,
    load_wallet_balance,
)

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

# ──────────────────────────────────────────────────────────────
# Configuration (all from .env)
# ──────────────────────────────────────────────────────────────

PRIVATE_KEY = os.environ.get("POLYBMICOB_PRIVATE_KEY", "")
FUNDER = os.environ.get("POLYMARKET_PROXY_WALLET", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "1"))
CHAIN_ID = int(os.environ.get("CHAIN_ID", "137"))
CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

# Trading parameters
MAX_TRADE_USD = float(os.environ.get("MAX_TRADE_USD", "1.00"))
MIN_BALANCE_USD = float(os.environ.get("MIN_BALANCE_USD", "3.00"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.10"))
MIN_EDGE_UP = float(os.environ.get("MIN_EDGE_UP", "") or os.environ.get("MIN_EDGE", "0.10"))
MIN_EDGE_DOWN = float(os.environ.get("MIN_EDGE_DOWN", "") or os.environ.get("MIN_EDGE", "0.10"))
MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "3.00"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "5"))
PAUSE_AFTER_LOSSES_SEC = int(os.environ.get("PAUSE_AFTER_LOSSES_SEC", "1800"))

# Timing
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "45"))
SCAN_MIN_MINUTES = float(os.environ.get("SCAN_MIN_MINUTES", "1.0"))
SCAN_MAX_MINUTES = float(os.environ.get("SCAN_MAX_MINUTES", "6.0"))
RATE_LIMIT_SEC = float(os.environ.get("RATE_LIMIT_SEC", "1.5"))
RPC_URL = os.environ.get("CHAINSTACK_NODE", "https://polygon-bor-rpc.publicnode.com")

# Auto-claim: check for redeemable positions every N cycles (~7.5 min at 45s interval)
CLAIM_EVERY_N_CYCLES = int(os.environ.get("CLAIM_EVERY_N_CYCLES", "10"))

# Hour-of-day filter (UTC hours when trading is allowed, empty = all hours)
# Based on backtest: best hours are 00,04,07,08,14,15,16,17,18 UTC
TRADING_HOURS_STR = os.environ.get("TRADING_HOURS_UTC", "")
TRADING_HOURS: set[int] | None = (
    {int(h.strip()) for h in TRADING_HOURS_STR.split(",") if h.strip()}
    if TRADING_HOURS_STR.strip()
    else None
)

# Minimum momentum to generate a pre-market signal (%)
MIN_MOMENTUM_PCT = float(os.environ.get("MIN_MOMENTUM_PCT", "0.05"))

# Fallback minimum order size (used if API doesn't return min_order_size)
# Note: Polymarket API returns actual min per market (often 2, sometimes 5)
MIN_ORDER_SIZE_FALLBACK = int(os.environ.get("MIN_ORDER_SIZE", "1"))

# Maximum execution price per share (skip if orderbook price too high)
# At $0.70: win=$1.50 loss=$3.50 per 5 shares. At $0.90: win=$0.50 loss=$4.50
MAX_EXEC_PRICE = float(os.environ.get("MAX_EXEC_PRICE", "0.70"))

# Minimum market volume (USD) to trade - filters out low-activity markets
# Low-volume markets have unreliable orderbooks and wide spreads
MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "0"))

# In-play mode: bet on markets already running (60-180s after start)
IN_PLAY_ENABLED = os.environ.get("IN_PLAY_ENABLED", "true").lower() == "true"
IN_PLAY_MIN_ELAPSED = int(os.environ.get("IN_PLAY_MIN_ELAPSED_SEC", "60"))
IN_PLAY_MAX_ELAPSED = int(os.environ.get("IN_PLAY_MAX_ELAPSED_SEC", "180"))
IN_PLAY_MIN_MOVE = float(os.environ.get("IN_PLAY_MIN_MOVE_PCT", "0.08"))

# Kelly criterion: dynamic position sizing based on edge and confidence
# Quarter-Kelly (0.25x) is conservative - protects against model errors
KELLY_ENABLED = os.environ.get("KELLY_ENABLED", "false").lower() == "true"
KELLY_MULTIPLIER = float(os.environ.get("KELLY_MULTIPLIER", "0.25"))
KELLY_MIN_USD = float(os.environ.get("KELLY_MIN_USD", "1.00"))  # Polymarket $1 minimum
KELLY_MAX_USD = float(os.environ.get("KELLY_MAX_USD", "5.00"))  # cap per trade

# Insurance bet: after momentum trade, buy $1 of cheap opposite side as reversal insurance
# Profitable when opposite token < $0.15 (EV +$0.66 at 15% reversal rate)
INSURANCE_ENABLED = os.environ.get("INSURANCE_ENABLED", "false").lower() == "true"
INSURANCE_BUDGET_USD = float(os.environ.get("INSURANCE_BUDGET_USD", "1.00"))
INSURANCE_MAX_PRICE = float(os.environ.get("INSURANCE_MAX_PRICE", "0.15"))

# Maker mode: use GTC post-only orders for pre-market (earn maker rebate instead of paying taker fee)
MAKER_MODE_ENABLED = os.environ.get("MAKER_MODE_ENABLED", "false").lower() == "true"
MAKER_TIMEOUT_SEC = int(os.environ.get("MAKER_TIMEOUT_SEC", "120"))

# Black-Scholes fair value: replace heuristic momentum probability with BS model
# Uses realized BTC volatility from 30x1m klines to compute mathematical probability
# that BTC continues in current direction for remaining time in 5-min window
BS_ENABLED = os.environ.get("BS_ENABLED", "false").lower() == "true"

# Flash crash detector: buy undervalued tokens when price drops without BTC justification
FLASH_CRASH_ENABLED = os.environ.get("FLASH_CRASH_ENABLED", "false").lower() == "true"
FLASH_CRASH_MIN_DROP = float(os.environ.get("FLASH_CRASH_MIN_DROP_PCT", "20.0"))
FLASH_CRASH_MAX_BTC = float(os.environ.get("FLASH_CRASH_MAX_BTC_PCT", "0.05"))
FLASH_CRASH_MAX_PRICE = float(os.environ.get("FLASH_CRASH_MAX_PRICE", "0.35"))
FLASH_CRASH_BUDGET = float(os.environ.get("FLASH_CRASH_BUDGET_USD", "1.00"))

# Early exit: sell positions early to cut losses (hold-to-resolution by default)
EARLY_EXIT_ENABLED = os.environ.get("EARLY_EXIT_ENABLED", "false").lower() == "true"
STOP_LOSS_THRESHOLD = float(os.environ.get("STOP_LOSS_THRESHOLD", "0.30"))
MOMENTUM_REVERSAL_PCT = float(os.environ.get("MOMENTUM_REVERSAL_PCT", "0.15"))

# Builder API credentials (for gasless claiming via relayer)
BUILDER_KEY = os.environ.get("POLY_BUILDER_API_KEY", "")
BUILDER_SECRET = os.environ.get("POLY_BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.environ.get("POLY_BUILDER_PASSPHRASE", "")

# Paths
DATA_DIR = Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "btc_trades.json"
LOG_FILE = DATA_DIR / "btc_bot.log"

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

DATA_DIR.mkdir(parents=True, exist_ok=True)

# RotatingFileHandler: 2 MB per file, keep 5 backups (btc_bot.log.1 .. .5)
_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5,
)
_file_handler.setFormatter(_log_formatter)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _stream_handler],
)
log = logging.getLogger("polybmicob")

# ──────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────

traded_slugs: set[str] = set()
daily_loss_usd: float = 0.0
consecutive_losses: int = 0
paused_until: float = 0.0
shutdown_requested: bool = False


def restore_state_from_trades() -> None:
    """
    Restore traded_slugs from trade history on startup.
    This prevents double-trading markets after a restart.
    Also restores daily loss tracking from today's trades.
    """
    global traded_slugs
    trades = load_trades()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in trades:
        slug = t.get("slug", "")
        if slug:
            traded_slugs.add(slug)
    today_trades = [t for t in trades if t.get("timestamp", "").startswith(today)]
    log.info(
        "Restored state: %d traded slugs, %d trades today",
        len(traded_slugs),
        len(today_trades),
    )


def _update_risk_state() -> None:
    """
    Recompute daily_loss_usd and consecutive_losses from today's resolved trades.

    Called after resolution checks. This is more robust than in-memory tracking
    because it survives restarts and reads the actual trade outcomes.
    """
    global daily_loss_usd, consecutive_losses, paused_until

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trades = load_trades()

    # Filter to today's resolved live trades
    today_resolved = [
        t for t in trades
        if t.get("timestamp", "").startswith(today)
        and t.get("resolved") is not None
        and not t.get("dry_run", True)
    ]

    # Compute daily loss (sum of negative PnL only)
    new_daily_loss = sum(
        abs(t.get("pnl", 0)) for t in today_resolved if not t.get("won", False)
    )

    # Compute consecutive losses (from most recent trades backwards)
    new_consec = 0
    for t in reversed(today_resolved):
        if not t.get("won", False):
            new_consec += 1
        else:
            break

    # Only log if values changed
    if new_daily_loss != daily_loss_usd or new_consec != consecutive_losses:
        daily_loss_usd = new_daily_loss
        consecutive_losses = new_consec
        log.info(
            "Risk state: daily_loss=$%.2f/%s%.2f, consec_losses=%d/%d",
            daily_loss_usd, "$", MAX_DAILY_LOSS_USD,
            consecutive_losses, MAX_CONSECUTIVE_LOSSES,
        )

    # Trigger pause if consecutive losses exceeded
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES and paused_until < time.time():
        paused_until = time.time() + PAUSE_AFTER_LOSSES_SEC
        log.warning(
            "PAUSING: %d consecutive losses, pausing for %ds",
            consecutive_losses, PAUSE_AFTER_LOSSES_SEC,
        )


def handle_shutdown(signum, frame):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    global shutdown_requested
    log.info("Shutdown requested (signal %d), finishing current cycle...", signum)
    shutdown_requested = True


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# ──────────────────────────────────────────────────────────────
# CLOB Client
# ──────────────────────────────────────────────────────────────

_clob_client: ClobClient | None = None


def get_clob_client() -> ClobClient:
    """Get or create the CLOB client (lazy init)."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    if not PRIVATE_KEY:
        raise RuntimeError(
            "POLYBMICOB_PRIVATE_KEY not set. "
            "Check .env file in polyclaw or polybmicob directory."
        )

    log.info("Initializing CLOB client (signature_type=%d)...", SIGNATURE_TYPE)
    client = ClobClient(
        CLOB_HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    log.info("CLOB client ready.")
    _clob_client = client
    return client


# ──────────────────────────────────────────────────────────────
# Trade logging
# ──────────────────────────────────────────────────────────────


def load_trades() -> list[dict]:
    """Load trade history from JSON file."""
    if not TRADES_FILE.exists():
        return []
    try:
        return json.loads(TRADES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_trade(trade: dict) -> None:
    """Append a trade to the JSON trade log (atomic write)."""
    trades = load_trades()
    trades.append(trade)
    tmp = TRADES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(trades, indent=2))
    tmp.rename(TRADES_FILE)


# ──────────────────────────────────────────────────────────────
# Orderbook analysis
# ──────────────────────────────────────────────────────────────


def get_orderbook_signal(
    client: ClobClient,
    up_token_id: str,
    down_token_id: str,
) -> OrderbookSignal | None:
    """Fetch orderbooks for both tokens and compute imbalance signal."""
    try:
        up_book = client.get_order_book(up_token_id)
        time.sleep(RATE_LIMIT_SEC)
        down_book = client.get_order_book(down_token_id)

        up_bids = [
            (float(b.price), float(b.size)) for b in (up_book.bids or [])
        ]
        up_asks = [
            (float(a.price), float(a.size)) for a in (up_book.asks or [])
        ]
        down_bids = [
            (float(b.price), float(b.size)) for b in (down_book.bids or [])
        ]
        down_asks = [
            (float(a.price), float(a.size)) for a in (down_book.asks or [])
        ]

        return compute_orderbook_imbalance(up_bids, up_asks, down_bids, down_asks)

    except Exception as exc:
        log.warning("Failed to get orderbook: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────
# Ghost order detection
# ──────────────────────────────────────────────────────────────


def _check_position_exists(token_id: str) -> bool:
    """
    Check if we already hold a position for this token on Polymarket.

    Used during order retries to detect ghost orders: if a "Request exception"
    occurred but the order actually went through on the server, we'd see a
    position here. This prevents double-ordering on retry.
    """
    if not FUNDER:
        return False
    try:
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user": FUNDER,
                "sizeThreshold": 0,
                "limit": 100,
            },
            timeout=10,
        )
        resp.raise_for_status()
        positions = resp.json()
        for pos in positions:
            # Check if any position's asset matches our token_id
            asset = pos.get("asset", "")
            if asset == token_id and float(pos.get("size", 0)) > 0:
                return True
        return False
    except Exception as exc:
        log.debug("Ghost order check failed: %s", exc)
        return False  # On error, assume no ghost (safer to not block retry)


# ──────────────────────────────────────────────────────────────
# Order execution
# ──────────────────────────────────────────────────────────────


def place_trade(
    client: ClobClient,
    signal: TradeSignal,
    size_usd: float,
    dry_run: bool = False,
) -> dict | None:
    """
    Place a FOK (Fill or Kill) buy order for the given signal.

    FOK = fill entirely and immediately, or cancel. No ghost orders.
    Uses best ask price as limit (slippage protection).

    Returns:
        Order result dict with 'filled'=True, or None on failure/no fill.
    """
    token_id = signal.token_id
    price = signal.entry_price

    try:
        # Fetch raw orderbook via httpx to get min_order_size + asks
        raw_resp = httpx.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        raw_resp.raise_for_status()
        raw_book = raw_resp.json()

        # Read dynamic min_order_size from API (fallback to env config)
        min_order_size = int(raw_book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))

        asks = sorted(raw_book.get("asks", []), key=lambda a: float(a["price"]))

        if not asks:
            log.info("  No asks on orderbook, skipping (no liquidity)")
            record_order_rejected("no_asks", "Empty orderbook")
            return None

        best_ask = float(asks[0]["price"])

        # For FOK: use best ask + slippage tolerance (sweep 1 cent above)
        # This allows filling across multiple ask levels for better fill rate
        slippage = 0.01
        exec_price = round(min(best_ask + slippage, 0.95), 2)
        exec_price = max(exec_price, 0.02)

        # MAX_EXEC_PRICE filter: skip if orderbook price is too expensive
        # Buying at $0.90 means risking $4.50 to win $0.50 (on 5 shares)
        if exec_price > MAX_EXEC_PRICE:
            log.info(
                "  Exec price $%.2f > $%.2f max, skipping (bad risk/reward: "
                "risk $%.2f to win $%.2f on %s)",
                exec_price, MAX_EXEC_PRICE,
                exec_price * min_order_size,
                (1.0 - exec_price) * min_order_size,
                signal.direction.upper(),
            )
            record_order_rejected("price_too_high", f"exec ${exec_price:.2f} > max ${MAX_EXEC_PRICE:.2f}")
            return None

        # Check available liquidity up to our price (may span multiple levels)
        available_size = sum(
            float(a["size"]) for a in asks if float(a["price"]) <= exec_price
        )

        # Calculate shares (enforce per-market minimum from API)
        size = round(size_usd / exec_price, 0)
        if size < min_order_size:
            size = min_order_size

        # Check if enough liquidity exists
        if available_size < size:
            log.info(
                "  Insufficient liquidity: need %.0f shares, only %.0f available @ $%.2f",
                size, available_size, exec_price,
            )
            record_order_rejected("low_liq", f"need {size:.0f}, only {available_size:.0f}")
            return None

        log.info(
            "  Placing %s %s (FOK): %.0f shares @ $%.2f ($%.2f total) [min_order=%d]",
            "DRY-RUN" if dry_run else "ORDER",
            signal.direction.upper(),
            size,
            exec_price,
            size * exec_price,
            min_order_size,
        )

        if dry_run:
            return {
                "orderID": "dry-run",
                "status": "dry-run",
                "exec_price": exec_price,
                "size": size,
                "filled": True,
            }

        order_args = OrderArgs(
            price=exec_price, size=size, side=BUY, token_id=token_id
        )

        # Retry with exponential backoff on API errors
        # Re-sign each attempt to avoid "Duplicated" rejection
        # IMPORTANT: Before each retry, check if previous order silently filled
        max_retries = 4
        backoff_schedule = [2, 5, 10, 15]  # seconds between retries
        result = None
        for attempt in range(max_retries + 1):
            try:
                signed = client.create_order(order_args)
                result = client.post_order(signed, OrderType.FOK)
                break
            except Exception as post_exc:
                err_msg = str(post_exc).lower()
                # Don't retry FOK kills (price moved) or balance errors - only network errors
                if "fully filled" in err_msg or "killed" in err_msg:
                    log.info("  FOK order killed (price moved / no liquidity), not retrying")
                    record_order_rejected("fok_killed", "Price moved during retry")
                    return None
                if "balance" in err_msg or "allowance" in err_msg:
                    log.warning("  Insufficient balance/allowance, not retrying")
                    record_order_rejected("no_balance", "Insufficient balance")
                    return None
                if attempt < max_retries:
                    wait = backoff_schedule[attempt] if attempt < len(backoff_schedule) else 15
                    log.warning("  Order attempt %d/%d failed: %s, retrying in %.0fs...",
                                attempt + 1, max_retries + 1, post_exc, wait)
                    time.sleep(wait)
                    # Check if the failed order actually went through (ghost order detection)
                    if _check_position_exists(token_id):
                        log.warning("  Ghost order detected: position already exists for token, aborting retry")
                        record_order_filled()
                        return {
                            "orderID": "ghost-detected",
                            "filled": True,
                            "size": size,
                            "exec_price": exec_price,
                        }
                else:
                    # Final attempt failed - still check for ghost order
                    if _check_position_exists(token_id):
                        log.warning("  Ghost order detected after all retries: position exists for token")
                        record_order_filled()
                        return {
                            "orderID": "ghost-detected",
                            "filled": True,
                            "size": size,
                            "exec_price": exec_price,
                        }
                    log.error("  Trade failed after %d attempts: %s", max_retries + 1, post_exc)
                    return None
        if result is None:
            return None

        order_id = result.get("orderID", result.get("id", "unknown"))
        status = result.get("status", "unknown")

        if status == "MATCHED" or result.get("success"):
            log.info("  ORDER FILLED (FOK): %s", order_id)
            record_order_filled()
            result["filled"] = True
            result["size"] = size
            result["exec_price"] = exec_price
            return result
        else:
            log.warning("  ORDER NOT FILLED (FOK): %s status=%s", order_id, status)
            record_order_rejected("not_filled", f"FOK status={status}")
            return None

    except Exception as exc:
        log.error("  Trade failed: %s", exc)
        record_order_rejected("error", str(exc)[:80])
        return None


# ──────────────────────────────────────────────────────────────
# Maker order execution (GTC post-only for pre-market)
# ──────────────────────────────────────────────────────────────


def place_maker_trade(
    client: ClobClient,
    signal: TradeSignal,
    size_usd: float,
    timeout_sec: int = 120,
    dry_run: bool = False,
) -> dict | None:
    """
    Place a GTC post-only buy order for pre-market signals (maker strategy).

    Post-only ensures the order rests on the book (maker) and never crosses
    the spread (which would make it a taker). Makers pay $0 fee on Polymarket
    crypto markets and earn a share of the 20% maker rebate pool.

    Strategy:
      1. Place GTC post-only at best bid + $0.01 (aggressive but still maker)
      2. Monitor order status every 5s for up to timeout_sec
      3. If filled -> return success
      4. If not filled by timeout -> cancel and return None

    Args:
        client: ClobClient instance.
        signal: TradeSignal with direction, token_id, entry_price.
        size_usd: Dollar amount to trade.
        timeout_sec: Max seconds to wait for fill (default 120).
        dry_run: If True, simulate without placing real orders.

    Returns:
        Order result dict with 'filled'=True, or None on failure/timeout.
    """
    token_id = signal.token_id

    try:
        # Fetch orderbook to determine maker price
        raw_resp = httpx.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        raw_resp.raise_for_status()
        raw_book = raw_resp.json()

        min_order_size = int(raw_book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))
        bids = sorted(raw_book.get("bids", []), key=lambda b: float(b["price"]), reverse=True)
        asks = sorted(raw_book.get("asks", []), key=lambda a: float(a["price"]))

        if not asks:
            log.info("  MAKER: no asks on orderbook, skipping (no liquidity)")
            record_order_rejected("no_asks", "Empty orderbook (maker)")
            return None

        best_ask = float(asks[0]["price"])
        best_bid = float(bids[0]["price"]) if bids else 0.01

        # Maker price: best bid + $0.01 (one tick above best bid)
        # Must stay below best ask to be post-only (otherwise it would cross spread)
        maker_price = round(best_bid + 0.01, 2)

        # Safety: if our maker price >= best ask, reduce to best ask - 0.01
        # This ensures post-only won't reject (crossing the spread = taker)
        if maker_price >= best_ask:
            maker_price = round(best_ask - 0.01, 2)

        maker_price = max(maker_price, 0.02)

        # MAX_EXEC_PRICE filter
        if maker_price > MAX_EXEC_PRICE:
            log.info(
                "  MAKER: price $%.2f > $%.2f max, skipping",
                maker_price, MAX_EXEC_PRICE,
            )
            record_order_rejected("price_too_high", f"maker ${maker_price:.2f} > max ${MAX_EXEC_PRICE:.2f}")
            return None

        # Calculate shares
        size = round(size_usd / maker_price, 0)
        if size < min_order_size:
            size = min_order_size

        log.info(
            "  Placing %s %s (GTC post-only): %.0f shares @ $%.2f ($%.2f total) "
            "[bid=$%.2f ask=$%.2f spread=$%.2f, timeout=%ds]",
            "DRY-RUN" if dry_run else "MAKER ORDER",
            signal.direction.upper(),
            size, maker_price, size * maker_price,
            best_bid, best_ask, best_ask - best_bid,
            timeout_sec,
        )

        if dry_run:
            return {
                "orderID": "dry-run-maker",
                "status": "dry-run",
                "exec_price": maker_price,
                "size": size,
                "filled": True,
                "mode": "maker",
            }

        # Create and post GTC post-only order
        order_args = OrderArgs(
            price=maker_price, size=size, side=BUY, token_id=token_id,
        )

        try:
            signed = client.create_order(order_args)
            result = client.post_order(signed, OrderType.GTC, post_only=True)
        except Exception as post_exc:
            err_msg = str(post_exc).lower()
            if "post only" in err_msg or "would match" in err_msg:
                log.info("  MAKER: post-only rejected (would cross spread), skipping")
                record_order_rejected("post_only_reject", "Would cross spread")
                return None
            if "balance" in err_msg or "allowance" in err_msg:
                log.warning("  MAKER: insufficient balance/allowance")
                record_order_rejected("no_balance", "Insufficient balance (maker)")
                return None
            log.error("  MAKER: order placement failed: %s", post_exc)
            record_order_rejected("error", str(post_exc)[:80])
            return None

        order_id = result.get("orderID", result.get("id", ""))
        if not order_id:
            log.warning("  MAKER: no order ID returned, aborting")
            return None

        log.info("  MAKER ORDER PLACED: %s (waiting for fill...)", order_id)

        # Monitor order status until filled or timeout
        start_time = time.time()
        poll_interval = 5  # seconds between status checks

        while time.time() - start_time < timeout_sec:
            if shutdown_requested:
                log.info("  MAKER: shutdown requested, cancelling order %s", order_id)
                try:
                    client.cancel(order_id)
                except Exception:
                    pass
                return None

            time.sleep(poll_interval)

            # Check if position exists (order filled)
            if _check_position_exists(token_id):
                elapsed = int(time.time() - start_time)
                log.info(
                    "  MAKER ORDER FILLED: %s (%ds wait, saved taker fee)",
                    order_id, elapsed,
                )
                record_order_filled()
                return {
                    "orderID": order_id,
                    "filled": True,
                    "size": size,
                    "exec_price": maker_price,
                    "mode": "maker",
                    "wait_sec": elapsed,
                }

            # Log progress every 30s
            elapsed = int(time.time() - start_time)
            if elapsed % 30 < poll_interval:
                log.info(
                    "  MAKER: waiting for fill... %ds/%ds elapsed",
                    elapsed, timeout_sec,
                )

        # Timeout reached - cancel the order
        elapsed = int(time.time() - start_time)
        log.info(
            "  MAKER TIMEOUT: order %s not filled after %ds, cancelling",
            order_id, elapsed,
        )
        try:
            client.cancel(order_id)
            log.info("  MAKER: order %s cancelled", order_id)
        except Exception as cancel_exc:
            log.warning("  MAKER: cancel failed: %s (may have filled)", cancel_exc)
            # Final check - maybe it filled during cancel
            if _check_position_exists(token_id):
                log.info("  MAKER: order filled during cancel! %s", order_id)
                record_order_filled()
                return {
                    "orderID": order_id,
                    "filled": True,
                    "size": size,
                    "exec_price": maker_price,
                    "mode": "maker",
                    "wait_sec": elapsed,
                }

        record_order_rejected("maker_timeout", f"Not filled in {timeout_sec}s")
        return None

    except Exception as exc:
        log.error("  MAKER trade failed: %s", exc)
        record_order_rejected("error", str(exc)[:80])
        return None


# ──────────────────────────────────────────────────────────────
# Early exit (sell) execution
# ──────────────────────────────────────────────────────────────


def place_sell_order(
    client: ClobClient,
    exit_signal: ExitSignal,
    dry_run: bool = False,
) -> dict | None:
    """
    Place a FOK sell order to exit a position early.

    Sells at best bid - slippage to ensure fill.

    Returns:
        Order result dict with 'filled'=True, or None on failure.
    """
    token_id = exit_signal.token_id
    sell_price = exit_signal.current_price  # best bid from early_exit module

    try:
        # Fetch fresh orderbook for bids
        raw_resp = httpx.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        raw_resp.raise_for_status()
        raw_book = raw_resp.json()

        min_order_size = int(raw_book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))

        bids = sorted(raw_book.get("bids", []), key=lambda b: float(b["price"]), reverse=True)
        if not bids:
            log.info("  SELL: No bids on orderbook for %s, cannot exit", exit_signal.slug)
            return None

        best_bid = float(bids[0]["price"])

        # Sell at best bid - 1 cent slippage for guaranteed fill
        exec_price = round(max(best_bid - 0.01, 0.01), 2)

        # Check bid liquidity
        available_size = sum(
            float(b["size"]) for b in bids if float(b["price"]) >= exec_price
        )

        size = float(exit_signal.shares)
        if size < min_order_size:
            size = float(min_order_size)

        if available_size < size:
            log.info(
                "  SELL: Insufficient bid liquidity for %s: need %.0f, only %.0f available",
                exit_signal.slug, size, available_size,
            )
            return None

        pnl_per_share = exec_price - exit_signal.entry_price
        total_pnl = pnl_per_share * size

        log.info(
            "  %s SELL %s (FOK): %.0f shares @ $%.2f (entry $%.2f, PnL $%+.2f) [%s]",
            "DRY-RUN" if dry_run else "EARLY EXIT",
            exit_signal.direction.upper(),
            size,
            exec_price,
            exit_signal.entry_price,
            total_pnl,
            exit_signal.trigger,
        )

        if dry_run:
            return {
                "orderID": "dry-run-sell",
                "status": "dry-run",
                "exec_price": exec_price,
                "size": size,
                "filled": True,
            }

        order_args = OrderArgs(
            price=exec_price, size=size, side=SELL, token_id=token_id
        )

        # Retry once on failure (sells are time-sensitive)
        result = None
        for attempt in range(2):
            try:
                signed = client.create_order(order_args)
                result = client.post_order(signed, OrderType.FOK)
                break
            except Exception as exc:
                err_str = str(exc).lower()
                if "balance" in err_str or "allowance" in err_str:
                    log.info(
                        "  Sell %s: no balance/allowance (tokens likely already resolved)",
                        exit_signal.slug,
                    )
                    return {"no_balance": True}
                if attempt == 0:
                    log.warning("  Sell attempt 1 failed: %s, retrying...", exc)
                    time.sleep(2)
                else:
                    log.error("  Sell failed after 2 attempts: %s", exc)
                    return None

        if result is None:
            return None

        order_id = result.get("orderID", result.get("id", "unknown"))
        status = result.get("status", "unknown")

        if status == "MATCHED" or result.get("success"):
            log.info("  SELL FILLED (FOK): %s  PnL: $%+.2f", order_id, total_pnl)
            result["filled"] = True
            result["exec_price"] = exec_price
            result["pnl"] = total_pnl
            return result
        else:
            log.warning("  SELL NOT FILLED (FOK): %s status=%s", order_id, status)
            return None

    except Exception as exc:
        log.error("  Sell order failed: %s", exc)
        return None


def _place_insurance_bet(
    client: ClobClient,
    slug: str,
    direction: str,
    market_data: dict,
    dry_run: bool = False,
) -> None:
    """
    Buy $1 of the cheap opposite side as reversal insurance.

    After a momentum trade (e.g. bought DOWN @$0.60), immediately buy
    $1 of the opposite (UP) if it's cheap enough (< INSURANCE_MAX_PRICE).

    Math at $0.09 entry, 15% reversal rate:
      Win: 11 shares x $0.91 = +$10.01
      Loss: 11 shares x $0.09 = -$0.99
      EV = 15% x $10.01 - 85% x $0.99 = +$0.66

    When momentum LOSES (-$3.00), insurance saves you 15% of the time:
      -$3.00 + $10.01 = +$7.01 (converts loss to big win)
    """
    try:
        # Determine opposite token ID from market data
        import json as _json
        mkt = market_data.get("market", {})
        token_ids_raw = mkt.get("clobTokenIds")
        if isinstance(token_ids_raw, str):
            token_ids = _json.loads(token_ids_raw)
        elif isinstance(token_ids_raw, list):
            token_ids = token_ids_raw
        else:
            token_ids = []

        if len(token_ids) < 2:
            # Fallback: look up from Gamma API
            resp = httpx.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return
            mkt = data[0].get("markets", [{}])[0]
            token_ids_raw = mkt.get("clobTokenIds")
            if isinstance(token_ids_raw, str):
                token_ids = _json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw or []
            if len(token_ids) < 2:
                return

        # token_ids[0] = UP, token_ids[1] = DOWN
        if direction == "down":
            opp_token_id = token_ids[0]  # buy UP as insurance
            opp_dir = "UP"
        else:
            opp_token_id = token_ids[1]  # buy DOWN as insurance
            opp_dir = "DOWN"

        # Get opposite orderbook
        book_resp = httpx.get(
            f"{CLOB_HOST}/book",
            params={"token_id": opp_token_id},
            timeout=10,
        )
        book_resp.raise_for_status()
        book = book_resp.json()
        asks = sorted(book.get("asks", []), key=lambda a: float(a["price"]))
        opp_min_order = int(book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))

        if not asks:
            log.info("  INSURANCE: no asks for opposite %s on %s", opp_dir, slug)
            return

        opp_best_ask = float(asks[0]["price"])

        # Only buy if cheap enough
        if opp_best_ask > INSURANCE_MAX_PRICE:
            log.info(
                "  INSURANCE: skip %s (opposite %s @ $%.2f > $%.2f max)",
                slug, opp_dir, opp_best_ask, INSURANCE_MAX_PRICE,
            )
            return

        opp_exec_price = round(min(opp_best_ask + 0.01, 0.95), 2)
        shares = int(INSURANCE_BUDGET_USD / opp_exec_price)

        # Ensure meets $1.00 minimum order value
        min_shares_for_value = math.ceil(1.00 / opp_exec_price) if opp_exec_price > 0 else 100
        shares = max(shares, opp_min_order, min_shares_for_value)
        total_cost = shares * opp_exec_price

        # Check liquidity
        available = sum(float(a["size"]) for a in asks if float(a["price"]) <= opp_exec_price)
        if available < shares:
            log.info("  INSURANCE: insufficient liquidity for %s (%d available, need %d)", slug, available, shares)
            return

        potential_win = (1.0 - opp_exec_price) * shares
        log.info(
            "  INSURANCE: buy %s on %s @ $%.2f (%d shares, $%.2f) -> win $%.2f / lose $%.2f",
            opp_dir, slug, opp_exec_price, shares, total_cost,
            potential_win, total_cost,
        )

        if dry_run:
            save_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "direction": opp_dir.lower(),
                "token_id": opp_token_id,
                "entry_price": opp_best_ask,
                "shares": shares,
                "exec_price": opp_exec_price,
                "edge": 0,
                "confidence": 0,
                "reason": f"INSURANCE: cheap {opp_dir} @ ${opp_exec_price:.2f}",
                "order_id": "dry-run-insurance",
                "dry_run": True,
                "mode": "insurance",
            })
            return

        order_args = OrderArgs(
            price=opp_exec_price, size=shares, side=BUY, token_id=opp_token_id,
        )
        signed = client.create_order(order_args)
        result = client.post_order(signed, OrderType.FOK)

        order_id = result.get("orderID", result.get("id", "unknown"))
        status = result.get("status", "unknown")

        if status == "MATCHED" or result.get("success"):
            log.info("  INSURANCE FILLED: %s %s @ $%.2f (%d shares, $%.2f)", opp_dir, slug, opp_exec_price, shares, total_cost)
            record_order_filled()
            save_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "direction": opp_dir.lower(),
                "token_id": opp_token_id,
                "entry_price": opp_best_ask,
                "shares": shares,
                "exec_price": opp_exec_price,
                "edge": 0,
                "confidence": 0,
                "reason": f"INSURANCE: cheap {opp_dir} @ ${opp_exec_price:.2f}",
                "order_id": order_id,
                "dry_run": False,
                "mode": "insurance",
            })
        else:
            log.info("  INSURANCE NOT FILLED: %s status=%s", order_id, status)

    except Exception as exc:
        log.debug("Insurance bet failed for %s: %s", slug, exc)


def _mark_trade_early_exit(slug: str, sell_price: float, pnl: float, trigger: str) -> None:
    """Mark a trade as early-exited in the trades file."""
    trades = load_trades()
    for t in trades:
        if t.get("slug") == slug and t.get("resolved") is None and not t.get("early_exit"):
            t["early_exit"] = True
            t["early_exit_price"] = sell_price
            t["early_exit_pnl"] = round(pnl, 4)
            t["early_exit_trigger"] = trigger
            t["early_exit_time"] = datetime.now(timezone.utc).isoformat()
            t["resolved"] = datetime.now(timezone.utc).isoformat()
            t["won"] = pnl > 0
            t["pnl"] = round(pnl, 4)
            break
    tmp = TRADES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(trades, indent=2))
    tmp.rename(TRADES_FILE)


# ──────────────────────────────────────────────────────────────
# Late-game hedge: buy opposite side cheap near market end
# ──────────────────────────────────────────────────────────────

# Maximum price for hedge (opposite side). At $0.35: pair cost = entry + 0.35
HEDGE_MAX_PRICE = float(os.environ.get("HEDGE_MAX_PRICE", "0.35"))
# How many seconds before market end to check for hedge opportunity
HEDGE_WINDOW_SEC = int(os.environ.get("HEDGE_WINDOW_SEC", "60"))
# Enable/disable hedge mode
HEDGE_ENABLED = os.environ.get("HEDGE_ENABLED", "true").lower() == "true"


def _check_late_hedge(
    client: ClobClient,
    dry_run: bool = False,
) -> None:
    """
    Check open positions for late-game hedge opportunities.

    For each unresolved trade, if the market is near its end (last 60s)
    and the opposite side is cheap (ask < HEDGE_MAX_PRICE), buy it.
    This locks in profit if pair cost < $1.00, or reduces max loss otherwise.

    Example:
      - Holding DOWN @$0.48 (5 shares = $2.40 invested)
      - Market ends in 30s, UP ask = $0.25
      - Buy 5 UP @$0.25 = $1.25
      - Total invested: $2.40 + $1.25 = $3.65
      - Guaranteed payout: 5 * $1.00 = $5.00
      - Locked profit: $5.00 - $3.65 = $1.35
    """
    trades = load_trades()
    now = int(time.time())

    # Find open (unresolved, non-dry-run) trades
    open_trades = [
        t for t in trades
        if t.get("resolved") is None
        and not t.get("dry_run", True)
        and not t.get("hedged", False)
    ]

    if not open_trades:
        return

    for trade in open_trades:
        slug = trade.get("slug", "")
        if not slug:
            continue

        # Parse market start time from slug: btc-updown-5m-{epoch}
        try:
            market_start = int(slug.split("-")[-1])
        except (ValueError, IndexError):
            continue

        # Market ends 5 minutes (300s) after start
        market_end = market_start + 300
        time_remaining = market_end - now

        # Only hedge in the last HEDGE_WINDOW_SEC seconds
        if time_remaining > HEDGE_WINDOW_SEC or time_remaining < 5:
            continue

        direction = trade.get("direction", "")
        entry_exec_price = trade.get("exec_price") or trade.get("entry_price", 0.5)
        shares = trade.get("shares", 5)

        # Determine opposite token
        # We need to look up the market to get both token IDs
        try:
            resp = httpx.get(
                f"https://gamma-api.polymarket.com/events",
                params={"slug": slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                continue

            mkt = data[0].get("markets", [{}])[0]
            import json as _json
            token_ids_raw = mkt.get("clobTokenIds")
            if isinstance(token_ids_raw, str):
                token_ids = _json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw or []

            if len(token_ids) < 2:
                continue

            # token_ids[0] = UP, token_ids[1] = DOWN
            if direction == "down":
                opposite_token_id = token_ids[0]  # UP
                opposite_dir = "UP"
            else:
                opposite_token_id = token_ids[1]  # DOWN
                opposite_dir = "DOWN"

        except Exception as exc:
            log.debug("Hedge: failed to get market data for %s: %s", slug, exc)
            continue

        # Get opposite side best ask
        try:
            book_resp = httpx.get(
                f"{CLOB_HOST}/book",
                params={"token_id": opposite_token_id},
                timeout=10,
            )
            book_resp.raise_for_status()
            book = book_resp.json()
            asks = sorted(book.get("asks", []), key=lambda a: float(a["price"]))

            if not asks:
                continue

            opp_best_ask = float(asks[0]["price"])
            opp_min_order = int(book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))

        except Exception as exc:
            log.debug("Hedge: failed to get opposite orderbook for %s: %s", slug, exc)
            continue

        # Check if hedge is worth it
        if opp_best_ask > HEDGE_MAX_PRICE:
            continue

        pair_cost = entry_exec_price + opp_best_ask

        # Skip hedge if pair_cost >= $1.00 (guaranteed loss, not a real hedge)
        if pair_cost >= 1.00:
            log.info(
                "  HEDGE: skip %s (pair_cost $%.2f >= $1.00, no profit to lock)",
                slug, pair_cost,
            )
            continue

        # Compute hedge execution price first (best ask + 1c slippage)
        opp_exec_price = round(min(opp_best_ask + 0.01, 0.95), 2)

        hedge_shares = shares
        # Ensure hedge order meets Polymarket $1.00 minimum order value
        min_hedge_value = 1.00
        min_shares_for_value = math.ceil(min_hedge_value / opp_exec_price) if opp_exec_price > 0 else 100
        hedge_shares = max(hedge_shares, opp_min_order, min_shares_for_value)

        locked_profit = (1.00 - pair_cost) * hedge_shares
        log.info(
            "  HEDGE: %s on %s @ $%.2f (%ds left) -> pair_cost $%.2f, "
            "locked profit $%.2f (%d shares, $%.2f total order)",
            opposite_dir, slug, opp_best_ask,
            time_remaining, pair_cost, locked_profit,
            hedge_shares, hedge_shares * opp_exec_price,
        )

        opp_size = hedge_shares

        # Check liquidity
        available = sum(float(a["size"]) for a in asks if float(a["price"]) <= opp_exec_price)
        if available < opp_size:
            log.info("  HEDGE: insufficient liquidity for %s (%d available, need %d)", slug, available, opp_size)
            continue

        log.info(
            "  Placing HEDGE %s (FOK): %d shares @ $%.2f ($%.2f total)",
            opposite_dir, opp_size, opp_exec_price, opp_size * opp_exec_price,
        )

        if dry_run:
            # Mark as hedged in trades
            _mark_trade_hedged(slug, opposite_token_id, opp_exec_price, opp_size, pair_cost)
            continue

        # Check if we already hold the opposite token (ghost order prevention)
        if _check_position_exists(opposite_token_id):
            log.info("  HEDGE: already hold opposite token for %s, skipping", slug)
            _mark_trade_hedged(slug, opposite_token_id, opp_exec_price, opp_size, pair_cost)
            continue

        try:
            order_args = OrderArgs(
                price=opp_exec_price, size=opp_size, side=BUY, token_id=opposite_token_id,
            )
            signed = client.create_order(order_args)
            result = client.post_order(signed, OrderType.FOK)

            order_id = result.get("orderID", result.get("id", "unknown"))
            status = result.get("status", "unknown")

            if status == "MATCHED" or result.get("success"):
                log.info("  HEDGE FILLED: %s  pair_cost=$%.2f", order_id, pair_cost)
                _mark_trade_hedged(slug, opposite_token_id, opp_exec_price, opp_size, pair_cost)
                record_order_filled()
            else:
                log.info("  HEDGE NOT FILLED: %s status=%s", order_id, status)
        except Exception as exc:
            err_msg = str(exc).lower()
            if "fully filled" in err_msg or "killed" in err_msg:
                log.info("  HEDGE FOK killed for %s (price moved)", slug)
            else:
                log.warning("  HEDGE failed for %s: %s", slug, exc)


def _mark_trade_hedged(slug: str, opp_token_id: str, opp_price: float, opp_shares: int, pair_cost: float) -> None:
    """Mark a trade as hedged in the trades file."""
    trades = load_trades()
    for t in trades:
        if t.get("slug") == slug and t.get("resolved") is None and not t.get("hedged"):
            t["hedged"] = True
            t["hedge_token_id"] = opp_token_id
            t["hedge_price"] = opp_price
            t["hedge_shares"] = opp_shares
            t["hedge_pair_cost"] = round(pair_cost, 4)
            t["hedge_time"] = datetime.now(timezone.utc).isoformat()
            break
    tmp = TRADES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(trades, indent=2))
    tmp.rename(TRADES_FILE)


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────


def run_cycle(dry_run: bool = False) -> None:
    """Run one scan-signal-trade cycle."""
    global daily_loss_usd, consecutive_losses, paused_until

    now = time.time()

    # Hour-of-day filter
    current_hour = datetime.now(timezone.utc).hour
    if TRADING_HOURS is not None and current_hour not in TRADING_HOURS:
        log.info("Hour %02d UTC not in trading hours, skipping.", current_hour)
        return

    # Check if paused after consecutive losses
    if now < paused_until:
        remaining = int(paused_until - now)
        log.info(
            "Paused for %d more seconds after %d consecutive losses",
            remaining,
            MAX_CONSECUTIVE_LOSSES,
        )
        return

    # ── 1. Scan for upcoming markets ─────────────────────────
    try:
        markets = scan_btc_5m_markets(SCAN_MIN_MINUTES, SCAN_MAX_MINUTES)
        log.info("Found %d upcoming BTC 5m markets", len(markets))
        record_cycle()
    except Exception as exc:
        log.error("Scanner failed: %s", exc)
        return

    if not markets:
        return

    # ── 2. Get BTC momentum ──────────────────────────────────
    try:
        snapshot = get_btc_momentum()
        log.info(
            "BTC $%.0f  momentum=%+.4f%%  trend=%s",
            snapshot.price,
            snapshot.momentum_5m,
            snapshot.trend,
        )
    except Exception as exc:
        log.error("Price feed failed: %s", exc)
        return

    # ── 2b. Momentum threshold filter ────────────────────────
    if abs(snapshot.momentum_5m) < MIN_MOMENTUM_PCT:
        log.info("  Momentum %.3f%% < %.2f%% threshold, skipping pre-market.", snapshot.momentum_5m, MIN_MOMENTUM_PCT)
        record_momentum_skip()
        return

    # ── 3. Get sentiment (non-critical) ──────────────────────
    fg = get_fear_greed_index()
    fg_value = fg.value if fg else None

    # ── 4. Check each market for signals ─────────────────────
    client = None  # lazy init

    for mkt in markets:
        if shutdown_requested:
            return

        if mkt.slug in traded_slugs:
            continue

        # Quality filters
        if mkt.liquidity < 500:
            log.info("  %s: skip (liquidity $%.0f < $500)", mkt.slug, mkt.liquidity)
            continue

        if MIN_VOLUME_USD > 0 and mkt.volume < MIN_VOLUME_USD:
            log.info("  %s: skip (volume $%.0f < $%.0f min)", mkt.slug, mkt.volume, MIN_VOLUME_USD)
            continue

        if mkt.up_price is not None and not (0.10 <= mkt.up_price <= 0.90):
            log.info("  %s: skip (Up price %.2f outside 0.10-0.90)", mkt.slug, mkt.up_price)
            continue

        # Get CLOB client (lazy init)
        if client is None:
            try:
                client = get_clob_client()
            except Exception as exc:
                log.error("CLOB client init failed: %s", exc)
                return

        # Get orderbook imbalance
        ob_signal = get_orderbook_signal(
            client, mkt.up_token_id, mkt.down_token_id
        )

        # Generate signal with lower threshold, then apply asymmetric filter
        effective_min_edge = min(MIN_EDGE_UP, MIN_EDGE_DOWN)
        sig = generate_signal(
            momentum_pct=snapshot.momentum_5m,
            trend=snapshot.trend,
            up_price=mkt.up_price,
            down_price=mkt.down_price,
            up_token_id=mkt.up_token_id,
            down_token_id=mkt.down_token_id,
            market_slug=mkt.slug,
            orderbook=ob_signal,
            fear_greed_value=fg_value,
            min_edge=effective_min_edge,
            bs_enabled=BS_ENABLED,
        )

        # Apply asymmetric edge threshold per direction
        if sig is not None:
            required_edge = MIN_EDGE_UP if sig.direction == "up" else MIN_EDGE_DOWN
            if sig.edge < required_edge:
                log.info(
                    "  %s: SKIP %s (edge %.1f%% < %.0f%% %s threshold)",
                    mkt.slug, sig.direction.upper(),
                    sig.edge * 100, required_edge * 100, sig.direction.upper(),
                )
                sig = None

        # Also generate a "what-if" signal at 0% edge for dry-run visibility
        if sig is None and dry_run:
            whatif = generate_signal(
                momentum_pct=snapshot.momentum_5m,
                trend=snapshot.trend,
                up_price=mkt.up_price,
                down_price=mkt.down_price,
                up_token_id=mkt.up_token_id,
                down_token_id=mkt.down_token_id,
                market_slug=mkt.slug,
                orderbook=ob_signal,
                fear_greed_value=fg_value,
                min_edge=0.0,  # show what we'd do at any edge
            )
            if whatif:
                potential_profit = (1.00 - whatif.entry_price) * MAX_TRADE_USD / whatif.entry_price
                potential_loss = MAX_TRADE_USD
                log.info(
                    "  %s: SKIP (edge %.1f%% < %.0f%% threshold)  "
                    "Would buy %s @ $%.2f -> win $%.2f / lose $%.2f  [%s]",
                    mkt.slug,
                    whatif.edge * 100,
                    MIN_EDGE * 100,
                    whatif.direction.upper(),
                    whatif.entry_price,
                    potential_profit,
                    potential_loss,
                    whatif.reason,
                )
            else:
                log.info(
                    "  %s: no edge (Up=%.2f, Down=%.2f, trend=%s%s)",
                    mkt.slug,
                    mkt.up_price or 0,
                    mkt.down_price or 0,
                    snapshot.trend,
                    f", ob={ob_signal.imbalance:+.2f}" if ob_signal else "",
                )
            continue

        if sig is None:
            log.info(
                "  %s: no signal (Up=%.2f, Down=%.2f%s)",
                mkt.slug,
                mkt.up_price or 0,
                mkt.down_price or 0,
                f", ob={ob_signal.imbalance:+.2f}" if ob_signal else "",
            )
            continue

        # Calculate expected profit for logging
        shares = MAX_TRADE_USD / sig.entry_price
        profit_if_win = (1.00 - sig.entry_price) * shares
        loss_if_lose = MAX_TRADE_USD

        log.info(
            "  SIGNAL: %s on %s  edge=%.1f%%  conf=%.0f%%",
            sig.direction.upper(),
            mkt.slug,
            sig.edge * 100,
            sig.confidence * 100,
        )
        record_pre_signal()
        log.info(
            "    Buy %s @ $%.2f (%.0f shares) -> WIN $%.2f / LOSE $%.2f  (EV: $%+.2f)",
            sig.direction.upper(),
            sig.entry_price,
            shares,
            profit_if_win,
            loss_if_lose,
            sig.confidence * profit_if_win - (1 - sig.confidence) * loss_if_lose,
        )
        log.info("    Reason: %s", sig.reason)

        # ── 5. Execute trade ─────────────────────────────────
        # Check daily loss limit
        if daily_loss_usd >= MAX_DAILY_LOSS_USD:
            log.warning(
                "Daily loss limit reached ($%.2f >= $%.2f). Stopping.",
                daily_loss_usd,
                MAX_DAILY_LOSS_USD,
            )
            return

        # Add slug to traded_slugs BEFORE placing order to prevent
        # duplicate orders from concurrent cycles during retry delays
        traded_slugs.add(mkt.slug)

        # Choose execution strategy: maker (GTC post-only) or taker (FOK)
        # Maker mode: $0 fee + maker rebate, but order may not fill
        # Taker mode: immediate fill, but pays taker fee (up to 1.56%)
        result = None
        trade_mode = "pre-market"
        if MAKER_MODE_ENABLED:
            log.info("    Using MAKER mode (GTC post-only, timeout=%ds)", MAKER_TIMEOUT_SEC)
            result = place_maker_trade(
                client, sig, MAX_TRADE_USD,
                timeout_sec=MAKER_TIMEOUT_SEC, dry_run=dry_run,
            )
            trade_mode = "pre-market-maker"
            if result is None:
                log.info("    Maker order not filled, no fallback to FOK")
        else:
            result = place_trade(client, sig, MAX_TRADE_USD, dry_run=dry_run)

        if result is None:
            # Order failed/timed out - remove slug so we can retry next cycle
            traded_slugs.discard(mkt.slug)

        if result is not None:
            order_id = result.get("orderID", result.get("id", "unknown"))

            # Log trade
            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "slug": mkt.slug,
                "direction": sig.direction,
                "token_id": sig.token_id,
                "entry_price": sig.entry_price,
                "shares": int(result.get("size", 5)),
                "exec_price": result.get("exec_price", sig.entry_price),
                "edge": round(sig.edge, 4),
                "confidence": round(sig.confidence, 4),
                "reason": sig.reason,
                "order_id": order_id,
                "dry_run": dry_run,
                "btc_price": snapshot.price,
                "momentum": round(snapshot.momentum_5m, 4),
                "fear_greed": fg_value,
                "mode": trade_mode,
            }
            save_trade(trade_record)

            time.sleep(RATE_LIMIT_SEC)


def main() -> None:
    """Main entry point."""
    # Dry-run if: --dry-run flag OR BOT_MODE != "live" in .env
    bot_mode = os.environ.get("BOT_MODE", "dry-run").strip().lower()
    dry_run = "--dry-run" in sys.argv or bot_mode != "live"

    # Build metadata (set by Dockerfile ARG -> ENV)
    app_version = os.environ.get("APP_VERSION", "dev")
    app_commit = os.environ.get("APP_GIT_COMMIT", "unknown")[:7]
    app_build = os.environ.get("APP_BUILD_DATE", "unknown")

    log.info("=" * 60)
    log.info("PolyBMiCoB - BTC Micro-Cycle Options Bot")
    log.info("Version: %s (commit %s, built %s)", app_version, app_commit, app_build)
    log.info("Mode: %s", "DRY RUN" if dry_run else "LIVE")
    log.info(
        "Config: max_trade=$%.2f, min_edge=%.0f%%, scan=%d-%dm, interval=%ds",
        MAX_TRADE_USD,
        MIN_EDGE * 100,
        SCAN_MIN_MINUTES,
        SCAN_MAX_MINUTES,
        SCAN_INTERVAL_SEC,
    )
    if EARLY_EXIT_ENABLED:
        log.info(
            "Early exit: ENABLED (stop-loss<$%.2f, reversal>%.2f%%)",
            STOP_LOSS_THRESHOLD, MOMENTUM_REVERSAL_PCT,
        )
    else:
        log.info("Early exit: disabled (hold-to-resolution)")
    if INSURANCE_ENABLED:
        log.info(
            "Insurance: ENABLED ($%.2f budget, max entry $%.2f)",
            INSURANCE_BUDGET_USD, INSURANCE_MAX_PRICE,
        )
    else:
        log.info("Insurance: disabled")
    if KELLY_ENABLED:
        log.info(
            "Kelly: ENABLED (%.2fx, $%.2f-$%.2f range)",
            KELLY_MULTIPLIER, KELLY_MIN_USD, KELLY_MAX_USD,
        )
    else:
        log.info("Kelly: disabled (fixed $%.2f/trade)", MAX_TRADE_USD)
    if FLASH_CRASH_ENABLED:
        log.info(
            "Flash crash: ENABLED (min drop %.0f%%, max BTC %.2f%%, max price $%.2f)",
            FLASH_CRASH_MIN_DROP, FLASH_CRASH_MAX_BTC, FLASH_CRASH_MAX_PRICE,
        )
    else:
        log.info("Flash crash: disabled")
    if MAKER_MODE_ENABLED:
        log.info(
            "Maker mode: ENABLED (GTC post-only, timeout=%ds, $0 fee + 20%% rebate)",
            MAKER_TIMEOUT_SEC,
        )
    else:
        log.info("Maker mode: disabled (FOK taker orders)")
    if BS_ENABLED:
        log.info("Black-Scholes: ENABLED (realized vol from 30x1m klines)")
    else:
        log.info("Black-Scholes: disabled (heuristic momentum)")
    if MIN_VOLUME_USD > 0:
        log.info("Volume filter: ENABLED (min $%.0f)", MIN_VOLUME_USD)
    else:
        log.info("Volume filter: disabled (all markets)")
    log.info("=" * 60)

    # Start BTC WebSocket price feed (background thread)
    btc_feed = BtcPriceFeed()
    try:
        btc_feed.start()
        # Wait briefly for first price
        for _ in range(30):
            if btc_feed.price > 0:
                break
            time.sleep(0.1)
        if btc_feed.price > 0:
            log.info("BTC WebSocket feed: $%.0f (live)", btc_feed.price)
        else:
            log.warning("BTC WebSocket feed: no price yet (will use REST fallback)")
    except Exception as exc:
        log.warning("BTC WebSocket feed failed to start: %s (using REST fallback)", exc)

    # Restore state from previous runs (prevents double-trading after restart)
    restore_state_from_trades()
    _update_risk_state()

    cycle_count = 0

    while not shutdown_requested:
        cycle_count += 1

        try:
            run_cycle(dry_run=dry_run)
        except Exception as exc:
            log.error("Cycle error: %s", exc, exc_info=True)

        # ── In-play scan (every cycle, respects trading hours) ──
        current_hour = datetime.now(timezone.utc).hour
        if not IN_PLAY_ENABLED:
            pass
        elif TRADING_HOURS is not None and current_hour not in TRADING_HOURS:
            pass  # quiet hour - skip in-play too
        else:
          try:
            in_play_markets = scan_in_play_markets(
                min_elapsed_sec=IN_PLAY_MIN_ELAPSED, max_elapsed_sec=IN_PLAY_MAX_ELAPSED,
            )
            if in_play_markets:
                log.info("In-play scan: %d market(s) in window", len(in_play_markets))
            for ip_mkt in in_play_markets:
                if shutdown_requested:
                    break
                ip_slug = ip_mkt["slug"]
                ip_elapsed = ip_mkt["elapsed"]
                if ip_slug in traded_slugs:
                    log.info("  In-play %s: skip (already traded), %ds elapsed", ip_slug, ip_elapsed)
                    continue

                # Volume filter for in-play markets
                if MIN_VOLUME_USD > 0:
                    ip_volume = float(ip_mkt.get("market", {}).get("volume", 0) or 0)
                    if ip_volume < MIN_VOLUME_USD:
                        log.info("  In-play %s: skip (volume $%.0f < $%.0f min), %ds elapsed", ip_slug, ip_volume, MIN_VOLUME_USD, ip_elapsed)
                        continue

                # Dynamic edge: lower threshold for early in-play (market hasn't adjusted yet)
                # <30s: 5% edge (market makers slow), 30-120s: 8%, >120s: 12%
                if ip_elapsed < 30:
                    ip_edge = 0.05
                elif ip_elapsed < 120:
                    ip_edge = 0.08
                else:
                    ip_edge = min(MIN_EDGE_UP, MIN_EDGE_DOWN)

                # Pass WS BTC price to avoid REST call (instant vs 10s)
                ws_btc = btc_feed.price if btc_feed.is_fresh() else None
                ip_signal = analyze_in_play(
                    ip_mkt, min_move_pct=IN_PLAY_MIN_MOVE, min_edge=ip_edge,
                    btc_current_price=ws_btc,
                )
                if ip_signal is None:
                    # analyze_in_play now logs the reason at INFO level
                    continue

                # Apply dynamic edge threshold (already computed above based on elapsed)
                if ip_signal.edge < ip_edge:
                    log.info(
                        "  In-play %s: SKIP %s (edge %.1f%% < %.0f%% threshold @%ds), BTC %+.3f%%",
                        ip_slug, ip_signal.direction.upper(),
                        ip_signal.edge * 100, ip_edge * 100,
                        ip_elapsed, ip_signal.btc_move_pct,
                    )
                    continue

                log.info(
                    "  IN-PLAY SIGNAL: %s on %s  edge=%.1f%%  BTC %+.3f%% (%ds elapsed)",
                    ip_signal.direction.upper(),
                    ip_signal.slug,
                    ip_signal.edge * 100,
                    ip_signal.btc_move_pct,
                    ip_signal.elapsed_sec,
                )
                record_inplay_signal(
                    direction=ip_signal.direction,
                    market=ip_signal.slug,
                    edge=f"{ip_signal.edge*100:.1f}%",
                    btc_move=f"{ip_signal.btc_move_pct:+.3f}%",
                    elapsed=f"{ip_signal.elapsed_sec}s elapsed",
                )

                # Create a TradeSignal-compatible object for place_trade
                ip_trade_signal = TradeSignal(
                    direction=ip_signal.direction,
                    token_id=ip_signal.token_id,
                    entry_price=ip_signal.entry_price,
                    edge=ip_signal.edge,
                    confidence=ip_signal.confidence,
                    reason=ip_signal.reason,
                    market_slug=ip_signal.slug,
                )

                # Check daily loss limit before in-play trades too
                if daily_loss_usd >= MAX_DAILY_LOSS_USD:
                    log.warning(
                        "Daily loss limit reached ($%.2f >= $%.2f). Skipping in-play.",
                        daily_loss_usd, MAX_DAILY_LOSS_USD,
                    )
                    break

                client = get_clob_client()

                # Add slug to traded_slugs BEFORE placing order to prevent
                # duplicate orders from concurrent cycles during retry delays
                traded_slugs.add(ip_signal.slug)

                # Kelly criterion: dynamic position sizing based on edge
                if KELLY_ENABLED:
                    kf = kelly_fraction(ip_signal.confidence, ip_signal.entry_price, KELLY_MULTIPLIER)
                    fee_rate = calculate_poly_fee_rate(ip_signal.entry_price)
                    # Adjust edge for fees before Kelly
                    net_edge = ip_signal.edge - fee_rate
                    if net_edge <= 0:
                        log.info(
                            "  In-play %s: SKIP (edge %.1f%% - fee %.2f%% = net %.1f%% <= 0)",
                            ip_slug, ip_signal.edge * 100, fee_rate * 100, net_edge * 100,
                        )
                        traded_slugs.discard(ip_signal.slug)
                        continue
                    # Use last known wallet balance for Kelly calculation
                    wallet_balance = load_wallet_balance().get("usdc_balance", 30.0)
                    kelly_usd = max(KELLY_MIN_USD, min(wallet_balance * kf, KELLY_MAX_USD))
                    log.info(
                        "    Kelly: f=%.1f%% of $%.0f = $%.2f (conf=%.0f%%, fee=%.2f%%)",
                        kf * 100, wallet_balance, kelly_usd,
                        ip_signal.confidence * 100, fee_rate * 100,
                    )
                    trade_size_usd = kelly_usd
                else:
                    trade_size_usd = MAX_TRADE_USD

                result = place_trade(client, ip_trade_signal, trade_size_usd, dry_run=dry_run)

                if result is None:
                    # Order failed - remove slug so we can retry
                    traded_slugs.discard(ip_signal.slug)

                if result is not None:
                    order_id = result.get("orderID", result.get("id", "unknown"))
                    save_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "slug": ip_signal.slug,
                        "direction": ip_signal.direction,
                        "token_id": ip_signal.token_id,
                        "entry_price": ip_signal.entry_price,
                        "shares": int(result.get("size", 5)),
                        "exec_price": result.get("exec_price", ip_signal.entry_price),
                        "edge": round(ip_signal.edge, 4),
                        "confidence": round(ip_signal.confidence, 4),
                        "reason": ip_signal.reason,
                        "order_id": order_id,
                        "dry_run": dry_run,
                        "btc_price": ip_signal.btc_now,
                        "momentum": round(ip_signal.btc_move_pct, 4),
                        "fear_greed": None,
                        "mode": "in-play",
                    })

                    # Insurance bet: buy $1 of cheap opposite side immediately
                    if INSURANCE_ENABLED:
                        try:
                            _place_insurance_bet(
                                client=client,
                                slug=ip_signal.slug,
                                direction=ip_signal.direction,
                                market_data=ip_mkt,
                                dry_run=dry_run,
                            )
                        except Exception as ins_exc:
                            log.debug("Insurance bet error: %s", ins_exc)
          except Exception as exc:
            log.warning("In-play scan error: %s", exc)

        # ── Late-game hedge check (every cycle if enabled) ────
        if HEDGE_ENABLED and not shutdown_requested:
            try:
                client = get_clob_client()
                _check_late_hedge(client, dry_run=dry_run)
            except Exception as exc:
                log.debug("Hedge check failed: %s", exc)

        # ── Flash crash detector (every cycle if enabled) ─────
        if FLASH_CRASH_ENABLED and not shutdown_requested:
            try:
                fc_signals = detect_flash_crashes(
                    btc_price_feed=btc_feed,
                    min_token_drop_pct=FLASH_CRASH_MIN_DROP,
                    max_btc_move_pct=FLASH_CRASH_MAX_BTC,
                    max_buy_price=FLASH_CRASH_MAX_PRICE,
                )
                for fc in fc_signals:
                    if shutdown_requested:
                        break
                    fc_slug_key = f"{fc.slug}-fc-{fc.direction}"
                    if fc_slug_key in traded_slugs:
                        continue
                    log.info("  FLASH CRASH: %s", fc.reason)
                    # Create TradeSignal for place_trade
                    fc_trade = TradeSignal(
                        direction=fc.direction,
                        token_id=fc.token_id,
                        entry_price=fc.current_price,
                        edge=fc.price_drop_pct / 100,
                        confidence=0.60,
                        reason=fc.reason,
                        market_slug=fc.slug,
                    )
                    client = get_clob_client()
                    traded_slugs.add(fc_slug_key)
                    result = place_trade(client, fc_trade, FLASH_CRASH_BUDGET, dry_run=dry_run)
                    if result and result.get("filled"):
                        order_id = result.get("orderID", "unknown")
                        save_trade({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "slug": fc.slug,
                            "direction": fc.direction,
                            "token_id": fc.token_id,
                            "entry_price": fc.current_price,
                            "shares": int(result.get("size", 5)),
                            "exec_price": result.get("exec_price", fc.current_price),
                            "edge": round(fc.price_drop_pct / 100, 4),
                            "confidence": 0.60,
                            "reason": fc.reason,
                            "order_id": order_id,
                            "dry_run": dry_run,
                            "btc_price": btc_feed.price,
                            "momentum": round(fc.btc_move_pct, 4),
                            "mode": "flash-crash",
                        })
                        log.info("  FLASH CRASH FILLED: %s %s @ $%.2f", fc.direction.upper(), fc.slug, fc.current_price)
                    else:
                        traded_slugs.discard(fc_slug_key)
                    time.sleep(RATE_LIMIT_SEC)
            except Exception as exc:
                log.debug("Flash crash scan error: %s", exc)

        # ── Early exit check (every cycle if enabled) ─────────
        if EARLY_EXIT_ENABLED and not shutdown_requested:
            try:
                trades = load_trades()
                exit_signals = check_early_exits(
                    trades=trades,
                    clob_host=CLOB_HOST,
                    stop_loss_threshold=STOP_LOSS_THRESHOLD,
                    momentum_reversal_pct=MOMENTUM_REVERSAL_PCT,
                    max_trade_usd=MAX_TRADE_USD,
                )
                for ex in exit_signals:
                    if shutdown_requested:
                        break
                    log.info("  EARLY EXIT: %s", ex.reason)
                    client = get_clob_client()
                    sell_result = place_sell_order(client, ex, dry_run=dry_run)
                    if sell_result and sell_result.get("filled"):
                        sell_pnl = sell_result.get("pnl", 0.0)
                        sell_exec = sell_result.get("exec_price", ex.current_price)
                        _mark_trade_early_exit(
                            slug=ex.slug,
                            sell_price=sell_exec,
                            pnl=sell_pnl,
                            trigger=ex.trigger,
                        )
                        log.info(
                            "  Early exit complete: %s sold @ $%.2f (PnL $%+.2f) [%s]",
                            ex.slug, sell_exec, sell_pnl, ex.trigger,
                        )
                    elif sell_result and sell_result.get("no_balance"):
                        # Tokens already redeemed/resolved - skip future checks
                        log.info(
                            "  Early exit %s: no balance (market likely resolved), skipping",
                            ex.slug,
                        )
                    time.sleep(RATE_LIMIT_SEC)
            except Exception as exc:
                log.warning("Early exit check failed: %s", exc)

        # ── Resolution check (every 5 cycles) ────────────────
        if cycle_count % 5 == 0:
            try:
                newly = resolve_trades(TRADES_FILE, rate_limit_sec=RATE_LIMIT_SEC)
                if newly > 0:
                    log.info("--- Resolved %d trade(s) ---", newly)
                    record_resolution(newly)
            except Exception as exc:
                log.debug("Resolution check failed: %s", exc)

            # Update daily loss + consecutive losses from resolved trades
            try:
                _update_risk_state()
            except Exception:
                pass

        # ── Auto-claim check (every N cycles) ────────────────
        if cycle_count % CLAIM_EVERY_N_CYCLES == 0 and FUNDER:
            try:
                log.info("--- Auto-claim check (cycle %d) ---", cycle_count)
                claim_all_winnings(
                    proxy_wallet=FUNDER,
                    private_key=PRIVATE_KEY,
                    builder_key=BUILDER_KEY,
                    builder_secret=BUILDER_SECRET,
                    builder_passphrase=BUILDER_PASSPHRASE,
                    dry_run=dry_run,
                )
            except Exception as exc:
                log.warning("Auto-claim failed (non-fatal): %s", exc)

            # ── Wallet balance check (same cadence as claim) ──
            try:
                # USDC.e on Polygon: balanceOf(proxy_wallet)
                usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                addr_padded = FUNDER[2:].lower().zfill(64)
                call_data = f"0x70a08231{addr_padded}"
                resp = httpx.post(
                    RPC_URL,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_call",
                        "params": [{"to": usdc_contract, "data": call_data}, "latest"],
                        "id": 1,
                    },
                    timeout=10,
                )
                hex_balance = resp.json().get("result", "0x0")
                usdc_balance = int(hex_balance, 16) / 1e6
                record_wallet_balance(usdc_balance)
                log.info("Wallet balance: $%.2f USDC", usdc_balance)
            except Exception as exc:
                log.debug("Wallet balance check failed: %s", exc)

        if shutdown_requested:
            break

        log.info("--- Sleeping %ds ---", SCAN_INTERVAL_SEC)
        # Sleep in small increments to respond to shutdown quickly
        for _ in range(SCAN_INTERVAL_SEC):
            if shutdown_requested:
                break
            time.sleep(1)

    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
