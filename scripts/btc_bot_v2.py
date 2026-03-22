#!/usr/bin/env python3
"""
PolyBMiCoB v2 - Event-Driven BTC Micro-Cycle Options Bot

Replaces the poll-sleep loop from v1 with an event-driven architecture:
  - EventBus: central event dispatcher (thread-safe queue)
  - MarketClock: precise 5-min slot timer (no more Gamma API polling for discovery)
  - BtcPriceFeed: Binance WS -> "btc_price" events
  - PolyWsFeed: Polymarket WS -> "orderbook_update", "best_bid_ask", "market_resolved"
  - Scheduled tasks: resolution check, claim winnings, wallet balance

Event flow:
  MarketClock --"market_tick"(pre_market)--> handle_pre_market()
  MarketClock --"market_tick"(in_play)-----> handle_in_play()
  MarketClock --"market_tick"(ending)------> handle_hedge_check()
  BtcPriceFeed -"btc_price"----------------> (updates shared state)
  PolyWsFeed --"orderbook_update"----------> handle_orderbook()
  PolyWsFeed --"market_resolved"-----------> handle_resolution()
  Scheduled: resolve_trades (every 30s), claim_winnings (every 5min)

All trading logic (place_trade, place_maker_trade, etc.) is reused from v1.

Usage:
  cd /path/to/polybmicob
  workon polybmicob
  python scripts/btc_bot_v2.py [--dry-run]
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
from py_clob_client.order_builder.constants import BUY

from lib.event_bus import EventBus
from lib.market_clock import MarketClock
from lib.poly_ws_feed import PolyWsFeed
from lib.ws_price_feed import BtcPriceFeed

from lib.btc_market_scanner import scan_btc_5m_markets
from lib.price_feed import get_btc_momentum, get_fear_greed_index
from lib.claim_winnings import claim_all_winnings
from lib.early_exit import check_early_exits
from lib.flash_crash_detector import detect_flash_crashes
from lib.in_play_engine import analyze_in_play, scan_in_play_markets
from lib.resolution_tracker import resolve_trades
from lib.signal_engine import (
    TradeSignal,
    compute_orderbook_imbalance,
    generate_signal,
    kelly_fraction,
    calculate_poly_fee_rate,
)
from lib.stats_collector import (
    record_cycle,
    record_momentum_skip,
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
# Configuration (reuse all v1 env vars)
# ──────────────────────────────────────────────────────────────

PRIVATE_KEY = os.environ.get("POLYBMICOB_PRIVATE_KEY", "")
FUNDER = os.environ.get("POLYMARKET_PROXY_WALLET", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "1"))
CHAIN_ID = int(os.environ.get("CHAIN_ID", "137"))
CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")

MAX_TRADE_USD = float(os.environ.get("MAX_TRADE_USD", "1.00"))
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.10"))
MIN_EDGE_UP = float(os.environ.get("MIN_EDGE_UP", "") or os.environ.get("MIN_EDGE", "0.10"))
MIN_EDGE_DOWN = float(os.environ.get("MIN_EDGE_DOWN", "") or os.environ.get("MIN_EDGE", "0.10"))
MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "3.00"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "5"))
PAUSE_AFTER_LOSSES_SEC = int(os.environ.get("PAUSE_AFTER_LOSSES_SEC", "1800"))
MIN_MOMENTUM_PCT = float(os.environ.get("MIN_MOMENTUM_PCT", "0.05"))
MAX_EXEC_PRICE = float(os.environ.get("MAX_EXEC_PRICE", "0.70"))
MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "0"))
MIN_ORDER_SIZE_FALLBACK = int(os.environ.get("MIN_ORDER_SIZE", "1"))

SCAN_MIN_MINUTES = float(os.environ.get("SCAN_MIN_MINUTES", "1.0"))
SCAN_MAX_MINUTES = float(os.environ.get("SCAN_MAX_MINUTES", "6.0"))
RATE_LIMIT_SEC = float(os.environ.get("RATE_LIMIT_SEC", "1.5"))
RPC_URL = os.environ.get("CHAINSTACK_NODE", "https://polygon-bor-rpc.publicnode.com")
CLAIM_EVERY_N_CYCLES = int(os.environ.get("CLAIM_EVERY_N_CYCLES", "10"))

TRADING_HOURS_STR = os.environ.get("TRADING_HOURS_UTC", "")
TRADING_HOURS: set[int] | None = (
    {int(h.strip()) for h in TRADING_HOURS_STR.split(",") if h.strip()}
    if TRADING_HOURS_STR.strip()
    else None
)

IN_PLAY_ENABLED = os.environ.get("IN_PLAY_ENABLED", "true").lower() == "true"
IN_PLAY_MIN_ELAPSED = int(os.environ.get("IN_PLAY_MIN_ELAPSED_SEC", "60"))
IN_PLAY_MAX_ELAPSED = int(os.environ.get("IN_PLAY_MAX_ELAPSED_SEC", "180"))
IN_PLAY_MIN_MOVE = float(os.environ.get("IN_PLAY_MIN_MOVE_PCT", "0.08"))

KELLY_ENABLED = os.environ.get("KELLY_ENABLED", "false").lower() == "true"
KELLY_MULTIPLIER = float(os.environ.get("KELLY_MULTIPLIER", "0.25"))
KELLY_MIN_USD = float(os.environ.get("KELLY_MIN_USD", "1.00"))
KELLY_MAX_USD = float(os.environ.get("KELLY_MAX_USD", "5.00"))

INSURANCE_ENABLED = os.environ.get("INSURANCE_ENABLED", "false").lower() == "true"
INSURANCE_BUDGET_USD = float(os.environ.get("INSURANCE_BUDGET_USD", "1.00"))
INSURANCE_MAX_PRICE = float(os.environ.get("INSURANCE_MAX_PRICE", "0.15"))

MAKER_MODE_ENABLED = os.environ.get("MAKER_MODE_ENABLED", "false").lower() == "true"
MAKER_TIMEOUT_SEC = int(os.environ.get("MAKER_TIMEOUT_SEC", "120"))

BS_ENABLED = os.environ.get("BS_ENABLED", "false").lower() == "true"
MAX_VOL_5M = float(os.environ.get("MAX_VOL_5M", "0.12"))  # max 5-min vol % (0=disabled, 0.12=default)
MM_PAIR_ENABLED = os.environ.get("MM_PAIR_ENABLED", "false").lower() == "true"

FLASH_CRASH_ENABLED = os.environ.get("FLASH_CRASH_ENABLED", "false").lower() == "true"
FLASH_CRASH_MIN_DROP = float(os.environ.get("FLASH_CRASH_MIN_DROP_PCT", "20.0"))
FLASH_CRASH_MAX_BTC = float(os.environ.get("FLASH_CRASH_MAX_BTC_PCT", "0.05"))
FLASH_CRASH_MAX_PRICE = float(os.environ.get("FLASH_CRASH_MAX_PRICE", "0.35"))
FLASH_CRASH_BUDGET = float(os.environ.get("FLASH_CRASH_BUDGET_USD", "1.00"))

EARLY_EXIT_ENABLED = os.environ.get("EARLY_EXIT_ENABLED", "false").lower() == "true"
STOP_LOSS_THRESHOLD = float(os.environ.get("STOP_LOSS_THRESHOLD", "0.30"))
MOMENTUM_REVERSAL_PCT = float(os.environ.get("MOMENTUM_REVERSAL_PCT", "0.15"))

HEDGE_MAX_PRICE = float(os.environ.get("HEDGE_MAX_PRICE", "0.35"))
HEDGE_WINDOW_SEC = int(os.environ.get("HEDGE_WINDOW_SEC", "60"))
HEDGE_ENABLED = os.environ.get("HEDGE_ENABLED", "true").lower() == "true"

BUILDER_KEY = os.environ.get("POLY_BUILDER_API_KEY", "")
BUILDER_SECRET = os.environ.get("POLY_BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.environ.get("POLY_BUILDER_PASSPHRASE", "")

DATA_DIR = Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "btc_trades.json"
LOG_FILE = DATA_DIR / "btc_bot.log"

# ──────────────────────────────────────────────────────────────
# Logging (same as v1)
# ──────────────────────────────────────────────────────────────

DATA_DIR.mkdir(parents=True, exist_ok=True)

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
# Shared State
# ──────────────────────────────────────────────────────────────

traded_slugs: set[str] = set()
daily_loss_usd: float = 0.0
consecutive_losses: int = 0
paused_until: float = 0.0
shutdown_requested: bool = False

# Shared references (set in main)
btc_feed: BtcPriceFeed | None = None
poly_feed: PolyWsFeed | None = None
bus: EventBus | None = None
dry_run: bool = True


# ──────────────────────────────────────────────────────────────
# Reuse v1 functions (import from btc_bot)
# These are self-contained and don't depend on the main loop
# ──────────────────────────────────────────────────────────────

# Import trading functions from v1 to avoid duplication
from scripts.btc_bot import (
    get_clob_client,
    load_trades,
    save_trade,
    get_orderbook_signal,
    place_trade,
    place_maker_trade,
    place_mm_pair,
    place_sell_order,
    restore_state_from_trades,
    _update_risk_state,
    _check_late_hedge,
    _place_insurance_bet,
    _mark_trade_early_exit,
)


# ──────────────────────────────────────────────────────────────
# Event Handlers
# ──────────────────────────────────────────────────────────────


def handle_market_tick(event_type: str, data: dict) -> None:
    """
    Handle market clock ticks.

    Dispatches to the appropriate handler based on phase:
      - pre_market: scan for upcoming markets, generate signals
      - in_play: analyze running markets for in-play edge
      - mid_play: second in-play check (market has more data)
      - ending: late-game hedge check
    """
    phase = data.get("phase", "")
    slug = data.get("slug", "")
    slot_ts = data.get("slot_ts", 0)

    log.info(
        "Market tick: %s phase=%s (%+ds to start, %ds to end)",
        slug, phase,
        data.get("seconds_to_start", 0),
        data.get("seconds_to_end", 0),
    )

    # Hour-of-day filter
    current_hour = datetime.now(timezone.utc).hour
    in_trading_hours = TRADING_HOURS is None or current_hour in TRADING_HOURS

    if not in_trading_hours and not MM_PAIR_ENABLED:
        log.info("Hour %02d UTC not in trading hours, skipping.", current_hour)
        return

    # Check pause
    if time.time() < paused_until:
        remaining = int(paused_until - time.time())
        log.info("Paused for %d more seconds", remaining)
        return

    if phase == "pre_market":
        if MM_PAIR_ENABLED:
            # MM pair always runs first (24/7, no filters needed)
            _handle_mm_only(slug, slot_ts)
        if in_trading_hours and slug not in traded_slugs:
            # Directional trading on top (only during trading hours, only if MM didn't fill)
            _handle_pre_market(slug, slot_ts)
    elif phase == "in_play":
        # At market start: try to complete MM pairs (buy missing side)
        if MM_PAIR_ENABLED:
            _complete_mm_pair(slug)
        _handle_in_play(slug, slot_ts)
    elif phase == "mid_play":
        _handle_in_play(slug, slot_ts)  # same logic, second check
    elif phase == "ending":
        _handle_ending(slug, slot_ts)


def _handle_pre_market(slug: str, slot_ts: int) -> None:
    """Pre-market phase: scan Gamma API for market, generate signal, trade."""
    global daily_loss_usd

    if slug in traded_slugs:
        log.info("  %s: already traded, skipping pre-market", slug)
        return

    record_cycle()

    # Daily loss check
    if daily_loss_usd >= MAX_DAILY_LOSS_USD:
        log.warning("Daily loss limit reached ($%.2f >= $%.2f)", daily_loss_usd, MAX_DAILY_LOSS_USD)
        return

    # Get BTC momentum
    try:
        snapshot = get_btc_momentum()
        log.info("BTC $%.0f  momentum=%+.4f%%  trend=%s", snapshot.price, snapshot.momentum_5m, snapshot.trend)
    except Exception as exc:
        log.error("Price feed failed: %s", exc)
        return

    # Momentum threshold
    if abs(snapshot.momentum_5m) < MIN_MOMENTUM_PCT:
        log.info("  Momentum %.3f%% < %.2f%% threshold, skipping.", snapshot.momentum_5m, MIN_MOMENTUM_PCT)
        record_momentum_skip()
        return

    # Volatility filter (ATR proxy) - skip when BTC is too choppy
    # High vol = high reversal probability = bad for momentum trades
    if MAX_VOL_5M > 0:
        from lib.bs_fair_value import compute_realized_volatility, SECONDS_PER_YEAR
        sigma_annual = compute_realized_volatility()
        if sigma_annual is not None:
            import math
            sigma_5m = sigma_annual * math.sqrt(300.0 / SECONDS_PER_YEAR) * 100  # as percent
            if sigma_5m > MAX_VOL_5M:
                log.info("  Vol %.3f%% > %.2f%% threshold, skipping (too choppy).", sigma_5m, MAX_VOL_5M)
                return
            log.info("  Vol check OK: %.3f%% <= %.2f%%", sigma_5m, MAX_VOL_5M)

    # Scan Gamma API for this specific market
    try:
        markets = scan_btc_5m_markets(SCAN_MIN_MINUTES, SCAN_MAX_MINUTES)
        log.info("Found %d upcoming BTC 5m markets", len(markets))
    except Exception as exc:
        log.error("Scanner failed: %s", exc)
        return

    if not markets:
        return

    # Sentiment
    fg = get_fear_greed_index()
    fg_value = fg.value if fg else None

    client = None

    for mkt in markets:
        if shutdown_requested:
            return
        if mkt.slug in traded_slugs:
            continue

        # Quality filters
        if mkt.liquidity < 500:
            continue
        if MIN_VOLUME_USD > 0 and mkt.volume < MIN_VOLUME_USD:
            continue
        if mkt.up_price is not None and not (0.10 <= mkt.up_price <= 0.90):
            continue

        if client is None:
            try:
                client = get_clob_client()
            except Exception as exc:
                log.error("CLOB client init failed: %s", exc)
                return

        # Subscribe to Polymarket WS for this market's tokens
        if poly_feed is not None:
            poly_feed.subscribe([mkt.up_token_id, mkt.down_token_id])

        ob_signal = get_orderbook_signal(client, mkt.up_token_id, mkt.down_token_id)

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

        # Asymmetric edge
        if sig is not None:
            required_edge = MIN_EDGE_UP if sig.direction == "up" else MIN_EDGE_DOWN
            if sig.edge < required_edge:
                log.info("  %s: SKIP %s (edge %.1f%% < %.0f%%)", mkt.slug, sig.direction.upper(), sig.edge * 100, required_edge * 100)
                sig = None

        if sig is None:
            continue

        shares = MAX_TRADE_USD / sig.entry_price
        profit_if_win = (1.00 - sig.entry_price) * shares
        loss_if_lose = MAX_TRADE_USD

        log.info("  SIGNAL: %s on %s  edge=%.1f%%  conf=%.0f%%", sig.direction.upper(), mkt.slug, sig.edge * 100, sig.confidence * 100)
        record_pre_signal()
        log.info("    Buy %s @ $%.2f -> WIN $%.2f / LOSE $%.2f", sig.direction.upper(), sig.entry_price, profit_if_win, loss_if_lose)

        traded_slugs.add(mkt.slug)

        result = None
        trade_mode = "pre-market"
        if MM_PAIR_ENABLED:
            # Two-sided MM: bid both UP and DOWN, profit from spread
            log.info("    Using MM PAIR mode (bid both sides, timeout=%ds)", MAKER_TIMEOUT_SEC)
            mm_result = place_mm_pair(
                client, mkt.up_token_id, mkt.down_token_id,
                MAX_TRADE_USD, mkt.slug,
                timeout_sec=MAKER_TIMEOUT_SEC, dry_run=dry_run,
            )
            if mm_result and mm_result.get("filled"):
                # MM pair filled one side - use that as our trade
                result = mm_result
                trade_mode = "mm-pair"
                # Override signal direction/token with what actually filled
                sig = TradeSignal(
                    direction=mm_result["direction"],
                    token_id=mm_result["token_id"],
                    entry_price=mm_result["exec_price"],
                    edge=sig.edge,
                    confidence=sig.confidence,
                    reason=f"MM-pair fill (pair_cost=${mm_result.get('pair_cost', 0):.2f}) {sig.reason}",
                    market_slug=mkt.slug,
                )
        elif MAKER_MODE_ENABLED:
            result = place_maker_trade(client, sig, MAX_TRADE_USD, timeout_sec=MAKER_TIMEOUT_SEC, dry_run=dry_run)
            trade_mode = "pre-market-maker"
        else:
            result = place_trade(client, sig, MAX_TRADE_USD, dry_run=dry_run)

        if result is None:
            traded_slugs.discard(mkt.slug)
            continue

        order_id = result.get("orderID", result.get("id", "unknown"))
        save_trade({
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
        })

        time.sleep(RATE_LIMIT_SEC)


def _complete_mm_pair(slug: str) -> None:
    """
    At market start: if we have only one side of an MM pair, buy the other via FOK.

    This converts a 50/50 directional bet into a guaranteed arb profit.
    Only buys if pair_cost < $1.00 (otherwise leave as directional).
    """
    import json as _json

    trades = load_trades()
    # Find mm-pair trades for this slug that aren't hedged yet
    mm_trades = [
        t for t in trades
        if t.get("slug") == slug
        and t.get("mode", "").startswith("mm-pair")
        and not t.get("hedged")
        and not t.get("resolved")
        and not t.get("dry_run", True)
    ]

    if not mm_trades:
        return

    for trade in mm_trades:
        direction = trade.get("direction", "")
        entry_price = trade.get("exec_price") or trade.get("entry_price", 0.50)

        # Get market token IDs
        try:
            resp = httpx.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                continue
            mkt = data[0].get("markets", [{}])[0]
            token_ids_raw = mkt.get("clobTokenIds")
            if isinstance(token_ids_raw, str):
                token_ids = _json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw or []
            if len(token_ids) < 2:
                continue
        except Exception:
            continue

        # Determine opposite side
        if direction == "down":
            opp_token = token_ids[0]  # UP
            opp_dir = "UP"
        else:
            opp_token = token_ids[1]  # DOWN
            opp_dir = "DOWN"

        # Check if we already hold opposite (both sides filled = arb complete!)
        from scripts.btc_bot import _check_position_exists, _mark_trade_hedged
        if _check_position_exists(opp_token):
            log.info("  PAIR COMPLETE: already hold both sides of %s (arb locked!)", slug)
            _mark_trade_hedged(slug, opp_token, 0, 0, entry_price)
            continue

        # Get opposite orderbook
        try:
            book_resp = httpx.get(
                f"{CLOB_HOST}/book",
                params={"token_id": opp_token},
                timeout=10,
            )
            book_resp.raise_for_status()
            book = book_resp.json()
            asks = sorted(book.get("asks", []), key=lambda a: float(a["price"]))
            if not asks:
                continue
            opp_best_ask = float(asks[0]["price"])
        except Exception:
            continue

        pair_cost = entry_price + opp_best_ask
        if pair_cost >= 1.00:
            log.info("  PAIR SKIP: %s pair_cost $%.2f >= $1.00 (no arb edge)", slug, pair_cost)
            continue

        # Buy opposite side via FOK
        opp_exec = round(min(opp_best_ask + 0.01, 0.95), 2)
        min_order = int(book.get("min_order_size", MIN_ORDER_SIZE_FALLBACK))
        shares = trade.get("shares", 5)
        shares = max(shares, min_order, math.ceil(1.00 / opp_exec))

        locked_profit = (1.00 - pair_cost) * shares
        log.info(
            "  PAIR COMPLETE: buy %s on %s @ $%.2f (pair_cost $%.2f, locked profit $%.2f)",
            opp_dir, slug, opp_exec, pair_cost, locked_profit,
        )

        if dry_run:
            _mark_trade_hedged(slug, opp_token, opp_exec, shares, pair_cost)
            continue

        try:
            client = get_clob_client()
            order_args = OrderArgs(
                price=opp_exec, size=shares, side=BUY, token_id=opp_token,
            )
            signed = client.create_order(order_args)
            result = client.post_order(signed, OrderType.FOK)
            status = result.get("status", "")
            if status == "MATCHED" or result.get("success"):
                log.info("  PAIR COMPLETE FILLED: %s %s @ $%.2f -> guaranteed profit $%.2f",
                         opp_dir, slug, opp_exec, locked_profit)
                _mark_trade_hedged(slug, opp_token, opp_exec, shares, pair_cost)
                record_order_filled()
                save_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "slug": slug, "direction": opp_dir.lower(),
                    "token_id": opp_token, "entry_price": opp_best_ask,
                    "shares": shares, "exec_price": opp_exec,
                    "edge": 0, "confidence": 1.0,
                    "reason": f"MM-pair completion (pair_cost=${pair_cost:.2f}, profit=${locked_profit:.2f})",
                    "order_id": result.get("orderID", "unknown"),
                    "dry_run": False, "mode": "mm-pair-complete",
                })
            else:
                log.info("  PAIR COMPLETE: %s FOK not filled (price moved)", opp_dir)
        except Exception as exc:
            log.debug("  PAIR COMPLETE failed: %s", exc)


def _handle_mm_only(slug: str, slot_ts: int) -> None:
    """Off-hours MM pair: bid both sides without directional signal. Runs 24/7."""
    if slug in traded_slugs:
        return

    if daily_loss_usd >= MAX_DAILY_LOSS_USD:
        return

    current_hour = datetime.now(timezone.utc).hour
    log.info("  MM-only mode (hour %02d, off-hours)", current_hour)

    try:
        markets = scan_btc_5m_markets(SCAN_MIN_MINUTES, SCAN_MAX_MINUTES)
    except Exception as exc:
        log.warning("  MM-only: scanner failed: %s", exc)
        return

    if not markets:
        return

    client = None
    for mkt in markets:
        if shutdown_requested:
            return
        if mkt.slug in traded_slugs:
            continue
        if mkt.liquidity < 500:
            continue

        if client is None:
            try:
                client = get_clob_client()
            except Exception as exc:
                log.error("CLOB client init failed: %s", exc)
                return

        traded_slugs.add(mkt.slug)
        result = place_mm_pair(
            client, mkt.up_token_id, mkt.down_token_id,
            MAX_TRADE_USD, mkt.slug,
            timeout_sec=MAKER_TIMEOUT_SEC, dry_run=dry_run,
        )

        if result is None or not result.get("filled"):
            traded_slugs.discard(mkt.slug)
            continue

        order_id = result.get("orderID", "unknown")
        save_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "slug": mkt.slug,
            "direction": result["direction"],
            "token_id": result["token_id"],
            "entry_price": result["exec_price"],
            "shares": int(result.get("size", 5)),
            "exec_price": result["exec_price"],
            "edge": 0,
            "confidence": 0.50,
            "reason": f"MM-pair off-hours (pair_cost=${result.get('pair_cost', 0):.2f})",
            "order_id": order_id,
            "dry_run": dry_run,
            "btc_price": btc_feed.price if btc_feed else 0,
            "momentum": 0,
            "mode": "mm-pair-offhours",
        })
        log.info("  MM-only FILLED: %s on %s @ $%.2f", result["direction"].upper(), mkt.slug, result["exec_price"])
        time.sleep(RATE_LIMIT_SEC)


def _handle_in_play(slug: str, slot_ts: int) -> None:
    """In-play phase: analyze running markets for momentum edge."""
    if not IN_PLAY_ENABLED:
        return

    if slug in traded_slugs:
        return

    if daily_loss_usd >= MAX_DAILY_LOSS_USD:
        return

    try:
        in_play_markets = scan_in_play_markets(
            min_elapsed_sec=IN_PLAY_MIN_ELAPSED,
            max_elapsed_sec=IN_PLAY_MAX_ELAPSED,
        )
    except Exception as exc:
        log.warning("In-play scan error: %s", exc)
        return

    if not in_play_markets:
        return

    log.info("In-play scan: %d market(s) in window", len(in_play_markets))

    for ip_mkt in in_play_markets:
        if shutdown_requested:
            break

        ip_slug = ip_mkt["slug"]
        ip_elapsed = ip_mkt["elapsed"]

        if ip_slug in traded_slugs:
            continue

        if MIN_VOLUME_USD > 0:
            ip_volume = float(ip_mkt.get("market", {}).get("volume", 0) or 0)
            if ip_volume < MIN_VOLUME_USD:
                continue

        # Dynamic edge threshold
        if ip_elapsed < 30:
            ip_edge = 0.05
        elif ip_elapsed < 120:
            ip_edge = 0.08
        else:
            ip_edge = min(MIN_EDGE_UP, MIN_EDGE_DOWN)

        ws_btc = btc_feed.price if btc_feed and btc_feed.is_fresh() else None
        ip_signal = analyze_in_play(ip_mkt, min_move_pct=IN_PLAY_MIN_MOVE, min_edge=ip_edge, btc_current_price=ws_btc)

        if ip_signal is None:
            continue

        if ip_signal.edge < ip_edge:
            continue

        log.info(
            "  IN-PLAY SIGNAL: %s on %s  edge=%.1f%%  BTC %+.3f%% (%ds elapsed)",
            ip_signal.direction.upper(), ip_signal.slug,
            ip_signal.edge * 100, ip_signal.btc_move_pct, ip_signal.elapsed_sec,
        )
        record_inplay_signal(
            direction=ip_signal.direction, market=ip_signal.slug,
            edge=f"{ip_signal.edge*100:.1f}%", btc_move=f"{ip_signal.btc_move_pct:+.3f}%",
            elapsed=f"{ip_signal.elapsed_sec}s elapsed",
        )

        ip_trade_signal = TradeSignal(
            direction=ip_signal.direction, token_id=ip_signal.token_id,
            entry_price=ip_signal.entry_price, edge=ip_signal.edge,
            confidence=ip_signal.confidence, reason=ip_signal.reason,
            market_slug=ip_signal.slug,
        )

        client = get_clob_client()
        traded_slugs.add(ip_signal.slug)

        # Kelly sizing
        if KELLY_ENABLED:
            kf = kelly_fraction(ip_signal.confidence, ip_signal.entry_price, KELLY_MULTIPLIER)
            fee_rate = calculate_poly_fee_rate(ip_signal.entry_price)
            net_edge = ip_signal.edge - fee_rate
            if net_edge <= 0:
                traded_slugs.discard(ip_signal.slug)
                continue
            wallet_balance = load_wallet_balance().get("usdc_balance", 30.0)
            trade_size_usd = max(KELLY_MIN_USD, min(wallet_balance * kf, KELLY_MAX_USD))
        else:
            trade_size_usd = MAX_TRADE_USD

        result = place_trade(client, ip_trade_signal, trade_size_usd, dry_run=dry_run)

        if result is None:
            traded_slugs.discard(ip_signal.slug)
            continue

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
            "mode": "in-play",
        })

        # Insurance
        if INSURANCE_ENABLED:
            try:
                _place_insurance_bet(client=client, slug=ip_signal.slug, direction=ip_signal.direction, market_data=ip_mkt, dry_run=dry_run)
            except Exception:
                pass


def _handle_ending(slug: str, slot_ts: int) -> None:
    """Ending phase: check for hedge opportunities + flash crashes."""
    # Late-game hedge
    if HEDGE_ENABLED:
        try:
            client = get_clob_client()
            _check_late_hedge(client, dry_run=dry_run)
        except Exception as exc:
            log.debug("Hedge check failed: %s", exc)

    # Flash crash detector
    if FLASH_CRASH_ENABLED and daily_loss_usd < MAX_DAILY_LOSS_USD:
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
                fc_trade = TradeSignal(
                    direction=fc.direction, token_id=fc.token_id,
                    entry_price=fc.current_price, edge=fc.price_drop_pct / 100,
                    confidence=0.60, reason=fc.reason, market_slug=fc.slug,
                )
                client = get_clob_client()
                traded_slugs.add(fc_slug_key)
                result = place_trade(client, fc_trade, FLASH_CRASH_BUDGET, dry_run=dry_run)
                if result and result.get("filled"):
                    save_trade({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "slug": fc.slug, "direction": fc.direction,
                        "token_id": fc.token_id, "entry_price": fc.current_price,
                        "shares": int(result.get("size", 5)),
                        "exec_price": result.get("exec_price", fc.current_price),
                        "edge": round(fc.price_drop_pct / 100, 4), "confidence": 0.60,
                        "reason": fc.reason, "order_id": result.get("orderID", "unknown"),
                        "dry_run": dry_run, "btc_price": btc_feed.price if btc_feed else 0,
                        "mode": "flash-crash",
                    })
                else:
                    traded_slugs.discard(fc_slug_key)
                time.sleep(RATE_LIMIT_SEC)
        except Exception as exc:
            log.debug("Flash crash scan error: %s", exc)


def handle_market_resolved(event_type: str, data: dict) -> None:
    """Handle real-time market resolution from Polymarket WS."""
    token_id = data.get("token_id", "")
    market_id = data.get("market", "")
    log.info("Market resolved (WS): token=%s market=%s", token_id[:20], market_id[:20])

    # Trigger immediate resolution check
    _scheduled_resolve()


# ──────────────────────────────────────────────────────────────
# Scheduled Tasks (periodic, not event-driven)
# ──────────────────────────────────────────────────────────────


def _scheduled_resolve() -> None:
    """Resolve trades and update risk state."""
    try:
        newly = resolve_trades(TRADES_FILE, rate_limit_sec=RATE_LIMIT_SEC)
        if newly > 0:
            log.info("--- Resolved %d trade(s) ---", newly)
            record_resolution(newly)
    except Exception as exc:
        log.debug("Resolution check failed: %s", exc)

    try:
        _update_risk_state()
    except Exception:
        pass


def _scheduled_claim() -> None:
    """Claim winnings and check wallet balance."""
    if not FUNDER:
        return

    try:
        log.info("--- Auto-claim check ---")
        claim_all_winnings(
            proxy_wallet=FUNDER, private_key=PRIVATE_KEY,
            builder_key=BUILDER_KEY, builder_secret=BUILDER_SECRET,
            builder_passphrase=BUILDER_PASSPHRASE, dry_run=dry_run,
        )
    except Exception as exc:
        log.warning("Auto-claim failed: %s", exc)

    # Wallet balance check
    try:
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        addr_padded = FUNDER[2:].lower().zfill(64)
        call_data = f"0x70a08231{addr_padded}"
        resp = httpx.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0", "method": "eth_call",
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


def _scheduled_early_exit() -> None:
    """Check for early exit opportunities."""
    if not EARLY_EXIT_ENABLED:
        return

    try:
        trades = load_trades()
        exit_signals = check_early_exits(
            trades=trades, clob_host=CLOB_HOST,
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
                _mark_trade_early_exit(slug=ex.slug, sell_price=sell_exec, pnl=sell_pnl, trigger=ex.trigger)
            time.sleep(RATE_LIMIT_SEC)
    except Exception as exc:
        log.warning("Early exit check failed: %s", exc)


# ──────────────────────────────────────────────────────────────
# Main (event-driven)
# ──────────────────────────────────────────────────────────────


def main() -> None:
    """Main entry point - event-driven architecture."""
    global btc_feed, poly_feed, bus, dry_run, shutdown_requested

    bot_mode = os.environ.get("BOT_MODE", "dry-run").strip().lower()
    dry_run = "--dry-run" in sys.argv or bot_mode != "live"

    app_version = os.environ.get("APP_VERSION", "dev")
    app_commit = os.environ.get("APP_GIT_COMMIT", "unknown")[:7]
    app_build = os.environ.get("APP_BUILD_DATE", "unknown")

    log.info("=" * 60)
    log.info("PolyBMiCoB v2 - Event-Driven BTC Bot")
    log.info("Version: %s (commit %s, built %s)", app_version, app_commit, app_build)
    log.info("Mode: %s", "DRY RUN" if dry_run else "LIVE")
    log.info("Architecture: EVENT-DRIVEN (EventBus + MarketClock + WS feeds)")
    log.info(
        "Config: max_trade=$%.2f, min_edge=%.0f%% (UP=%.0f%% DOWN=%.0f%%), max_exec=$%.2f",
        MAX_TRADE_USD, MIN_EDGE * 100, MIN_EDGE_UP * 100, MIN_EDGE_DOWN * 100, MAX_EXEC_PRICE,
    )
    log.info(
        "Risk: max_daily_loss=$%.2f, max_consec_losses=%d, pause=%ds",
        MAX_DAILY_LOSS_USD, MAX_CONSECUTIVE_LOSSES, PAUSE_AFTER_LOSSES_SEC,
    )
    if TRADING_HOURS is not None:
        log.info("Trading hours (UTC): %s", ",".join(str(h) for h in sorted(TRADING_HOURS)))
    else:
        log.info("Trading hours: ALL (no filter)")
    if IN_PLAY_ENABLED:
        log.info(
            "In-play: ENABLED (elapsed %d-%ds, min_move=%.2f%%, dynamic edge 5%%/8%%/12%%)",
            IN_PLAY_MIN_ELAPSED, IN_PLAY_MAX_ELAPSED, IN_PLAY_MIN_MOVE,
        )
    else:
        log.info("In-play: disabled")
    if KELLY_ENABLED:
        log.info(
            "Kelly: ENABLED (%.2fx, $%.2f-$%.2f range)",
            KELLY_MULTIPLIER, KELLY_MIN_USD, KELLY_MAX_USD,
        )
    else:
        log.info("Kelly: disabled (fixed $%.2f/trade)", MAX_TRADE_USD)
    if INSURANCE_ENABLED:
        log.info(
            "Insurance: ENABLED ($%.2f budget, max entry $%.2f)",
            INSURANCE_BUDGET_USD, INSURANCE_MAX_PRICE,
        )
    else:
        log.info("Insurance: disabled")
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
    if MAX_VOL_5M > 0:
        log.info("Volatility filter: ENABLED (max 5m vol %.2f%%, skip choppy markets)", MAX_VOL_5M)
    else:
        log.info("Volatility filter: disabled")
    if MM_PAIR_ENABLED:
        log.info("MM Pair: ENABLED (bid both UP+DOWN, profit from spread, $0 maker fee)")
    else:
        log.info("MM Pair: disabled (directional trading only)")
    if HEDGE_ENABLED:
        log.info(
            "Hedge: ENABLED (max_price=$%.2f, window=%ds)",
            HEDGE_MAX_PRICE, HEDGE_WINDOW_SEC,
        )
    else:
        log.info("Hedge: disabled")
    if FLASH_CRASH_ENABLED:
        log.info(
            "Flash crash: ENABLED (min drop %.0f%%, max BTC %.2f%%, max price $%.2f)",
            FLASH_CRASH_MIN_DROP, FLASH_CRASH_MAX_BTC, FLASH_CRASH_MAX_PRICE,
        )
    else:
        log.info("Flash crash: disabled")
    if EARLY_EXIT_ENABLED:
        log.info(
            "Early exit: ENABLED (stop-loss<$%.2f, reversal>%.2f%%)",
            STOP_LOSS_THRESHOLD, MOMENTUM_REVERSAL_PCT,
        )
    else:
        log.info("Early exit: disabled (hold-to-resolution)")
    if MIN_VOLUME_USD > 0:
        log.info("Volume filter: ENABLED (min $%.0f)", MIN_VOLUME_USD)
    else:
        log.info("Volume filter: disabled (all markets)")
    log.info("=" * 60)

    # ── 1. Create EventBus ────────────────────────────────────
    bus = EventBus()

    # ── 2. Start BTC WebSocket feed (emits "btc_price" events) ──
    btc_feed = BtcPriceFeed(bus=bus)
    btc_feed.start()
    for _ in range(30):
        if btc_feed.price > 0:
            break
        time.sleep(0.1)
    if btc_feed.price > 0:
        log.info("BTC WebSocket feed: $%.0f (live)", btc_feed.price)
    else:
        log.warning("BTC WebSocket feed: no price yet (will use REST fallback)")

    # ── 3. Start Polymarket WebSocket feed ────────────────────
    poly_feed = PolyWsFeed(bus=bus)
    poly_feed.start()
    time.sleep(1)  # brief wait for connection
    if poly_feed.connected:
        log.info("Polymarket WebSocket feed: connected")
    else:
        log.warning("Polymarket WebSocket feed: not yet connected (will retry)")

    # ── 4. Start Market Clock (emits "market_tick" events) ────
    clock = MarketClock(
        bus=bus,
        pre_market_sec=30,    # 30s before slot: pre-market scan
        mid_play_sec=90,      # 90s after slot: in-play analysis
        ending_sec=60,        # 60s before end: hedge check
    )
    clock.start()

    # ── 5. Register event handlers ────────────────────────────
    bus.on("market_tick", handle_market_tick)
    bus.on("market_resolved", handle_market_resolved)

    # ── 6. Schedule periodic tasks ────────────────────────────
    bus.schedule(interval_sec=30, handler=_scheduled_resolve, name="resolve_trades")
    bus.schedule(interval_sec=300, handler=_scheduled_claim, name="claim_winnings")
    if EARLY_EXIT_ENABLED:
        bus.schedule(interval_sec=10, handler=_scheduled_early_exit, name="early_exit")

    # ── 7. Restore state from previous runs ───────────────────
    restore_state_from_trades()
    _update_risk_state()

    # ── 8. Signal handlers ────────────────────────────────────
    def _handle_signal(signum, frame):
        global shutdown_requested
        log.info("Shutdown requested (signal %d)", signum)
        shutdown_requested = True
        bus.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── 9. Run event loop (blocks until stop) ─────────────────
    log.info("Event loop starting...")
    try:
        bus.run()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received")
    finally:
        log.info("Shutting down...")
        clock.stop()
        poly_feed.stop()
        btc_feed.stop()
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()
