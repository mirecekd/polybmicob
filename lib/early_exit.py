"""
Early Exit Module for PolyBMiCoB.

Monitors open positions and triggers sell orders when:
  1. Stop-loss: token price drops below threshold (default 30c)
  2. Momentum reversal: BTC reverses direction while position could lock profit

Default behavior is HOLD-TO-RESOLUTION. This module only intervenes
on losing trades to cut losses early.

Why this helps:
  - With 84% WR, winning trades are untouched (token rises to ~$1.00)
  - Losing trades (16%) currently lose full stake (~50c per trade)
  - Stop-loss at 30c recovers ~20c per losing trade
  - Estimated improvement: +$2-3 per 57 trades on historical data
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("polybmicob.earlyexit")


@dataclass
class ExitSignal:
    """Signal to sell an open position early."""

    slug: str
    token_id: str
    direction: str
    entry_price: float
    current_price: float
    shares: int
    reason: str
    trigger: str  # "stop_loss" or "momentum_reversal"


def _get_token_best_bid(token_id: str, clob_host: str) -> float | None:
    """Get best bid price for a token from CLOB orderbook."""
    try:
        resp = httpx.get(
            f"{clob_host}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        if not bids:
            return None
        best = max(bids, key=lambda b: float(b["price"]))
        return float(best["price"])
    except Exception as exc:
        log.debug("Failed to get best bid for token: %s", exc)
        return None


def _get_token_mid_price(token_id: str, clob_host: str) -> float | None:
    """Get mid price (avg of best bid and best ask) for a token."""
    try:
        resp = httpx.get(
            f"{clob_host}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
        return (best_bid + best_ask) / 2.0
    except Exception as exc:
        log.debug("Failed to get mid price for token: %s", exc)
        return None


def _get_btc_momentum_since(start_price: float) -> float | None:
    """Get current BTC price change % since a given start price."""
    try:
        resp = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        resp.raise_for_status()
        current = float(resp.json()["price"])
        return ((current - start_price) / start_price) * 100
    except Exception as exc:
        log.debug("Failed to get BTC price: %s", exc)
        return None


def check_early_exits(
    trades: list[dict],
    clob_host: str = "https://clob.polymarket.com",
    stop_loss_threshold: float = 0.30,
    momentum_reversal_pct: float = 0.15,
    max_trade_usd: float = 1.00,
) -> list[ExitSignal]:
    """
    Check open (unresolved) positions for early exit triggers.

    Only checks LIVE (non-dry-run) trades that haven't been resolved yet
    and haven't already been early-exited.

    Args:
        trades: Full trade list from btc_trades.json.
        clob_host: CLOB API base URL.
        stop_loss_threshold: Sell if token price drops below this (default 0.30).
        momentum_reversal_pct: Sell if BTC reversed by this % (default 0.15%).
        max_trade_usd: Max trade size (to estimate shares held).

    Returns:
        List of ExitSignal for positions that should be sold.
    """
    exits = []

    # Find open positions: not resolved, not dry-run, not already early-exited
    open_trades = [
        t for t in trades
        if t.get("resolved") is None
        and not t.get("dry_run", True)
        and not t.get("early_exit", False)
    ]

    if not open_trades:
        return exits

    for trade in open_trades:
        token_id = trade.get("token_id", "")
        slug = trade.get("slug", "")
        direction = trade.get("direction", "")
        entry_price = trade.get("entry_price", 0.5)
        btc_price_at_entry = trade.get("btc_price", 0)

        if not token_id or not slug:
            continue

        # Get current token price (mid price for evaluation, best bid for selling)
        current_price = _get_token_mid_price(token_id, clob_host)
        if current_price is None:
            log.debug("  Early exit %s: no price data, skipping", slug)
            continue

        best_bid = _get_token_best_bid(token_id, clob_host)
        if best_bid is None or best_bid < 0.01:
            log.debug("  Early exit %s: no bids, skipping", slug)
            continue

        # Estimate shares held
        shares = max(int(round(max_trade_usd / entry_price)), 1)

        # ── Trigger 1: Stop-loss ──────────────────────────────
        if current_price < stop_loss_threshold:
            reason = (
                f"STOP-LOSS: {direction.upper()} token dropped to "
                f"${current_price:.2f} < ${stop_loss_threshold:.2f} threshold "
                f"(entry ${entry_price:.2f}, saving ~${entry_price - best_bid:.2f}/share)"
            )
            exits.append(ExitSignal(
                slug=slug,
                token_id=token_id,
                direction=direction,
                entry_price=entry_price,
                current_price=best_bid,
                shares=shares,
                reason=reason,
                trigger="stop_loss",
            ))
            continue  # Don't check other triggers for same trade

        # ── Trigger 2: Momentum reversal ──────────────────────
        if btc_price_at_entry and btc_price_at_entry > 0:
            btc_change = _get_btc_momentum_since(btc_price_at_entry)
            if btc_change is not None:
                # Check if BTC moved AGAINST our direction
                reversed_direction = (
                    (direction == "down" and btc_change > momentum_reversal_pct)
                    or (direction == "up" and btc_change < -momentum_reversal_pct)
                )
                # Only sell on reversal if we're still in profit (token > entry)
                still_in_profit = current_price > entry_price

                if reversed_direction and still_in_profit:
                    reason = (
                        f"MOMENTUM REVERSAL: BTC moved {btc_change:+.3f}% against "
                        f"{direction.upper()} position, locking profit "
                        f"(entry ${entry_price:.2f} -> sell ~${best_bid:.2f})"
                    )
                    exits.append(ExitSignal(
                        slug=slug,
                        token_id=token_id,
                        direction=direction,
                        entry_price=entry_price,
                        current_price=best_bid,
                        shares=shares,
                        reason=reason,
                        trigger="momentum_reversal",
                    ))

    return exits
