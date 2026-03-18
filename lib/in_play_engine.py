"""
In-Play Engine for PolyBMiCoB.

Analyzes markets that are ALREADY RUNNING (60-180s after start) and
compares real BTC price movement since market open with current token
prices to find mispricings.

Strategy:
  1. Find markets that started 60-180 seconds ago
  2. Get BTC price at market start time and current price
  3. Calculate real BTC movement since market open
  4. Compare with current token prices on CLOB
  5. If real movement suggests higher probability than market price -> bet

Example:
  Market started 90 seconds ago.
  BTC at start: $70,000. BTC now: $70,120 (+0.17%).
  Up token price: $0.55 (implying 55% probability).
  Our estimate: 62% probability Up (based on +0.17% move in first 90s).
  Edge: 62% - 55% = 7% -> BET UP.

Why this works:
  - Real price data is more predictive than pre-market momentum
  - Market makers don't update prices instantly
  - First 60-90s of movement is a strong predictor of 5-min outcome
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger("polybmicob.inplay")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class InPlaySignal:
    """Signal from in-play analysis of a running market."""

    slug: str
    direction: str  # "up" or "down"
    token_id: str
    entry_price: float
    edge: float
    confidence: float
    reason: str
    btc_at_start: float
    btc_now: float
    btc_move_pct: float
    elapsed_sec: int
    condition_id: str


def _get_btc_price_at(timestamp_sec: int) -> float | None:
    """
    Get BTC/USDT price at a specific timestamp using Binance klines.

    Fetches the 1-minute candle that contains the given timestamp.
    """
    try:
        ts_ms = timestamp_sec * 1000
        resp = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": ts_ms,
                "limit": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0][1])  # open price of the candle
    except Exception as exc:
        log.debug("Failed to get BTC price at %d: %s", timestamp_sec, exc)
    return None


def _get_btc_current_price() -> float | None:
    """Get current BTC/USDT price from Binance."""
    try:
        resp = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as exc:
        log.debug("Failed to get current BTC price: %s", exc)
    return None


def scan_in_play_markets(
    min_elapsed_sec: int = 60,
    max_elapsed_sec: int = 180,
) -> list[dict]:
    """
    Find BTC 5m markets that are currently running within the time window.

    Args:
        min_elapsed_sec: Minimum seconds since market start (default 60).
        max_elapsed_sec: Maximum seconds since market start (default 180).

    Returns:
        List of dicts with market data + elapsed time.
    """
    now = int(time.time())
    markets = []

    # Check the current and previous 5-min slots
    for offset in range(0, 3):
        ts = now - (now % 300) - (offset * 300)
        elapsed = now - ts

        if elapsed < min_elapsed_sec or elapsed > max_elapsed_sec:
            continue

        slug = f"btc-updown-5m-{ts}"

        try:
            resp = httpx.get(
                f"{GAMMA_API_BASE}/events",
                params={"slug": slug},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.debug("Failed to fetch %s: %s", slug, exc)
            continue

        if not data:
            continue

        event = data[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            continue

        mkt = event_markets[0]

        if mkt.get("closed"):
            continue  # Already resolved

        markets.append({
            "slug": slug,
            "start_ts": ts,
            "elapsed": elapsed,
            "market": mkt,
        })

    return markets


def analyze_in_play(
    market_data: dict,
    min_move_pct: float = 0.08,
    min_edge: float = 0.05,
) -> InPlaySignal | None:
    """
    Analyze a running market for in-play edge.

    Compares real BTC movement since market start with current token prices.

    Args:
        market_data: Dict from scan_in_play_markets().
        min_move_pct: Minimum BTC move % to generate signal (default 0.08%).
        min_edge: Minimum edge to generate signal (default 5%).

    Returns:
        InPlaySignal or None.
    """
    slug = market_data["slug"]
    start_ts = market_data["start_ts"]
    elapsed = market_data["elapsed"]
    mkt = market_data["market"]

    # Get token IDs and current prices
    import json
    token_ids_raw = mkt.get("clobTokenIds")
    if isinstance(token_ids_raw, str):
        try:
            token_ids = json.loads(token_ids_raw)
        except (json.JSONDecodeError, ValueError):
            return None
    elif isinstance(token_ids_raw, list):
        token_ids = token_ids_raw
    else:
        return None

    if not token_ids or len(token_ids) < 2:
        return None

    prices_raw = mkt.get("outcomePrices")
    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except (json.JSONDecodeError, ValueError):
            return None
    elif isinstance(prices_raw, list):
        prices = prices_raw
    else:
        return None

    if not prices or len(prices) < 2:
        return None

    try:
        up_price = float(prices[0])
        down_price = float(prices[1])
    except (ValueError, TypeError):
        return None

    # Get BTC price at market start and now
    btc_start = _get_btc_price_at(start_ts)
    if btc_start is None:
        return None

    btc_now = _get_btc_current_price()
    if btc_now is None:
        return None

    # Calculate move since market start
    move_pct = ((btc_now - btc_start) / btc_start) * 100

    # Skip if move is too small (noise)
    if abs(move_pct) < min_move_pct:
        log.info(
            "  In-play %s: skip (BTC move %+.3f%% < %.2f%% threshold), %ds elapsed",
            slug, move_pct, min_move_pct, elapsed,
        )
        return None

    # Estimate true probability based on real movement
    # The further BTC has moved, the more likely it stays in that direction
    # Scale: 0.1% move -> ~60% confidence, 0.3% move -> ~70%, 0.5%+ -> ~75%
    abs_move = abs(move_pct)
    if abs_move >= 0.5:
        confidence_boost = 0.25
    elif abs_move >= 0.3:
        confidence_boost = 0.20
    elif abs_move >= 0.2:
        confidence_boost = 0.15
    elif abs_move >= 0.1:
        confidence_boost = 0.10
    else:
        confidence_boost = 0.05

    # Time decay: the more time elapsed, the more likely current direction holds
    # 60s elapsed: small boost, 120s+: bigger boost
    time_factor = min(elapsed / 180.0, 1.0)  # 0.33 at 60s, 0.67 at 120s, 1.0 at 180s
    confidence_boost *= (0.7 + 0.3 * time_factor)

    # Polymarket price momentum: use market price itself as signal
    # If UP dropped from 0.50 to 0.30, the market "knows" DOWN is winning
    # Weight market signal more than BTC movement alone
    market_momentum = abs(up_price - 0.50)  # How far from 50/50
    if market_momentum > 0.10:
        # Market has moved significantly - trust it more
        market_confidence = 0.50 + market_momentum * 0.8  # 0.10->0.58, 0.20->0.66, 0.30->0.74
        # Blend BTC-based estimate with market-based estimate
        btc_confidence = 0.50 + confidence_boost
        # Market gets more weight as it moves further from 50/50
        market_weight = min(market_momentum * 3, 0.7)  # Up to 70% weight to market
        estimated_prob_blended = (
            market_weight * market_confidence
            + (1 - market_weight) * btc_confidence
        )
    else:
        estimated_prob_blended = 0.50 + confidence_boost

    btc_going_up = move_pct > 0
    if btc_going_up:
        estimated_prob = estimated_prob_blended
        market_prob = up_price
        direction = "up"
        token_id = token_ids[0]
        entry_price = up_price
    else:
        estimated_prob = estimated_prob_blended
        market_prob = down_price
        direction = "down"
        token_id = token_ids[1]
        entry_price = down_price

    edge = estimated_prob - market_prob

    if edge < min_edge:
        log.info(
            "  In-play %s: low edge %.1f%% < %.0f%% (BTC %+.3f%%, %s mkt=%.2f est=%.2f), %ds elapsed",
            slug, edge * 100, min_edge * 100,
            move_pct, direction.upper(), market_prob, estimated_prob, elapsed,
        )
        return None

    reason = (
        f"IN-PLAY: BTC {move_pct:+.3f}% since start ({elapsed}s ago), "
        f"est {estimated_prob:.0%} {direction.upper()} vs market {market_prob:.0%}"
    )

    condition_id = mkt.get("conditionId", "")

    return InPlaySignal(
        slug=slug,
        direction=direction,
        token_id=token_id,
        entry_price=entry_price,
        edge=edge,
        confidence=estimated_prob,
        reason=reason,
        btc_at_start=btc_start,
        btc_now=btc_now,
        btc_move_pct=move_pct,
        elapsed_sec=elapsed,
        condition_id=condition_id,
    )
