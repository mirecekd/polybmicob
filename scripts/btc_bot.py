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
from py_clob_client.order_builder.constants import BUY

from lib.btc_market_scanner import scan_btc_5m_markets
from lib.price_feed import get_btc_momentum, get_fear_greed_index
from lib.claim_winnings import claim_all_winnings
from lib.in_play_engine import analyze_in_play, scan_in_play_markets
from lib.resolution_tracker import resolve_trades
from lib.signal_engine import (
    OrderbookSignal,
    TradeSignal,
    compute_orderbook_imbalance,
    generate_signal,
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
MIN_ORDER_SIZE_FALLBACK = int(os.environ.get("MIN_ORDER_SIZE", "5"))

# In-play mode: bet on markets already running (60-180s after start)
IN_PLAY_ENABLED = os.environ.get("IN_PLAY_ENABLED", "true").lower() == "true"
IN_PLAY_MIN_ELAPSED = int(os.environ.get("IN_PLAY_MIN_ELAPSED_SEC", "60"))
IN_PLAY_MAX_ELAPSED = int(os.environ.get("IN_PLAY_MAX_ELAPSED_SEC", "180"))
IN_PLAY_MIN_MOVE = float(os.environ.get("IN_PLAY_MIN_MOVE_PCT", "0.08"))

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
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
            return None

        best_ask = float(asks[0]["price"])

        # For FOK: use best ask + slippage tolerance (sweep 1 cent above)
        # This allows filling across multiple ask levels for better fill rate
        slippage = 0.01
        exec_price = round(min(best_ask + slippage, 0.95), 2)
        exec_price = max(exec_price, 0.02)

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
            return None

        log.info(
            "  Placing %s %s (FOK): %.0f shares @ $%.2f ($%.2f total)",
            "DRY-RUN" if dry_run else "ORDER",
            signal.direction.upper(),
            size,
            exec_price,
            size * exec_price,
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

        # Retry with backoff on API errors (re-sign each attempt to avoid "Duplicated" rejection)
        max_retries = 2
        result = None
        for attempt in range(max_retries + 1):
            try:
                signed = client.create_order(order_args)
                result = client.post_order(signed, OrderType.FOK)
                break
            except Exception as post_exc:
                if attempt < max_retries:
                    wait = RATE_LIMIT_SEC * (attempt + 1)
                    log.warning("  Order attempt %d/%d failed: %s, retrying in %.1fs...",
                                attempt + 1, max_retries + 1, post_exc, wait)
                    time.sleep(wait)
                else:
                    log.error("  Trade failed after %d attempts: %s", max_retries + 1, post_exc)
                    return None
        if result is None:
            return None

        order_id = result.get("orderID", result.get("id", "unknown"))
        status = result.get("status", "unknown")

        if status == "MATCHED" or result.get("success"):
            log.info("  ORDER FILLED (FOK): %s", order_id)
            result["filled"] = True
            return result
        else:
            log.warning("  ORDER NOT FILLED (FOK): %s status=%s", order_id, status)
            return None

    except Exception as exc:
        log.error("  Trade failed: %s", exc)
        return None


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

        result = place_trade(client, sig, MAX_TRADE_USD, dry_run=dry_run)

        if result is not None:
            order_id = result.get("orderID", result.get("id", "unknown"))
            traded_slugs.add(mkt.slug)

            # Log trade
            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "slug": mkt.slug,
                "direction": sig.direction,
                "token_id": sig.token_id,
                "entry_price": sig.entry_price,
                "edge": round(sig.edge, 4),
                "confidence": round(sig.confidence, 4),
                "reason": sig.reason,
                "order_id": order_id,
                "dry_run": dry_run,
                "btc_price": snapshot.price,
                "momentum": round(snapshot.momentum_5m, 4),
                "fear_greed": fg_value,
            }
            save_trade(trade_record)

            time.sleep(RATE_LIMIT_SEC)


def main() -> None:
    """Main entry point."""
    # Dry-run if: --dry-run flag OR BOT_MODE != "live" in .env
    bot_mode = os.environ.get("BOT_MODE", "dry-run").strip().lower()
    dry_run = "--dry-run" in sys.argv or bot_mode != "live"

    log.info("=" * 60)
    log.info("PolyBMiCoB - BTC Micro-Cycle Options Bot")
    log.info("Mode: %s", "DRY RUN" if dry_run else "LIVE")
    log.info(
        "Config: max_trade=$%.2f, min_edge=%.0f%%, scan=%d-%dm, interval=%ds",
        MAX_TRADE_USD,
        MIN_EDGE * 100,
        SCAN_MIN_MINUTES,
        SCAN_MAX_MINUTES,
        SCAN_INTERVAL_SEC,
    )
    log.info("=" * 60)

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

        # ── In-play scan (every cycle) ───────────────────────
        if not IN_PLAY_ENABLED:
            pass
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
                ip_edge = min(MIN_EDGE_UP, MIN_EDGE_DOWN)
                ip_signal = analyze_in_play(
                    ip_mkt, min_move_pct=IN_PLAY_MIN_MOVE, min_edge=ip_edge,
                )
                if ip_signal is None:
                    # analyze_in_play now logs the reason at INFO level
                    continue

                # Asymmetric edge for in-play too
                ip_required = MIN_EDGE_UP if ip_signal.direction == "up" else MIN_EDGE_DOWN
                if ip_signal.edge < ip_required:
                    log.info(
                        "  In-play %s: SKIP %s (edge %.1f%% < %.0f%% %s threshold), BTC %+.3f%%",
                        ip_slug, ip_signal.direction.upper(),
                        ip_signal.edge * 100, ip_required * 100,
                        ip_signal.direction.upper(), ip_signal.btc_move_pct,
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

                client = get_clob_client()
                result = place_trade(client, ip_trade_signal, MAX_TRADE_USD, dry_run=dry_run)

                if result is not None:
                    order_id = result.get("orderID", result.get("id", "unknown"))
                    traded_slugs.add(ip_signal.slug)
                    save_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "slug": ip_signal.slug,
                        "direction": ip_signal.direction,
                        "token_id": ip_signal.token_id,
                        "entry_price": ip_signal.entry_price,
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
          except Exception as exc:
            log.warning("In-play scan error: %s", exc)

        # ── Resolution check (every 5 cycles) ────────────────
        if cycle_count % 5 == 0:
            try:
                newly = resolve_trades(TRADES_FILE, rate_limit_sec=RATE_LIMIT_SEC)
                if newly > 0:
                    log.info("--- Resolved %d trade(s) ---", newly)
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
