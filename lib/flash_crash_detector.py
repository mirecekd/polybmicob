"""
Flash Crash Detector for PolyBMiCoB.

Monitors Polymarket token prices and detects anomalies where token price
drops significantly without matching BTC movement. These are mean-reversion
opportunities where someone panic-sold or a market maker pulled liquidity.

Strategy:
  1. Track token prices over time (using Gamma API outcomePrices)
  2. Compare token price change with BTC price change
  3. If token dropped >20% but BTC moved <0.05% -> ANOMALY
  4. Buy the undervalued token (mean reversion)

Example:
  - UP token was $0.50, now $0.25 (dropped 50%)
  - BTC only moved -0.02% (basically flat)
  - UP token is massively oversold -> BUY for mean reversion
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("polybmicob.flashcrash")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class FlashCrashSignal:
    """Signal from flash crash detection."""

    slug: str
    direction: str  # which side to buy ("up" or "down")
    token_id: str
    current_price: float  # current cheap price
    previous_price: float  # what it was before crash
    price_drop_pct: float  # how much it dropped (%)
    btc_move_pct: float  # how much BTC moved (should be small)
    reason: str


def detect_flash_crashes(
    btc_price_feed,  # BtcPriceFeed instance for real-time BTC price
    min_token_drop_pct: float = 20.0,
    max_btc_move_pct: float = 0.05,
    max_buy_price: float = 0.35,
) -> list[FlashCrashSignal]:
    """
    Scan current and recent 5-min markets for flash crash opportunities.

    Compares Gamma API token prices with BTC price movement.
    If token price dropped significantly without BTC justification -> signal.

    Args:
        btc_price_feed: BtcPriceFeed with real-time BTC price.
        min_token_drop_pct: Minimum token price drop to trigger (default 20%).
        max_btc_move_pct: Maximum BTC move to qualify as anomaly (default 0.05%).
        max_buy_price: Maximum price to buy crashed token (default $0.35).

    Returns:
        List of FlashCrashSignal for detected anomalies.
    """
    signals = []
    now = int(time.time())

    # Check current and next 5-min slots
    for offset in range(0, 3):
        ts = now - (now % 300) + (offset * 300)
        elapsed = now - ts

        # Only check markets that are running (0-300s after start)
        if elapsed < 0 or elapsed > 300:
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
        except Exception:
            continue

        if not data:
            continue

        event = data[0]
        event_markets = event.get("markets", [])
        if not event_markets:
            continue

        mkt = event_markets[0]
        if mkt.get("closed"):
            continue

        # Get token prices
        prices_raw = mkt.get("outcomePrices")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        elif isinstance(prices_raw, list):
            prices = prices_raw
        else:
            continue

        if not prices or len(prices) < 2:
            continue

        try:
            up_price = float(prices[0])
            down_price = float(prices[1])
        except (ValueError, TypeError):
            continue

        # Get token IDs
        token_ids_raw = mkt.get("clobTokenIds")
        if isinstance(token_ids_raw, str):
            try:
                token_ids = json.loads(token_ids_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        elif isinstance(token_ids_raw, list):
            token_ids = token_ids_raw
        else:
            continue

        if len(token_ids) < 2:
            continue

        # Check if BTC price feed is available
        if not btc_price_feed or not btc_price_feed.is_fresh():
            continue

        # Get BTC price at market start for comparison
        btc_now = btc_price_feed.price

        # Detect anomaly: one side is very cheap (< max_buy_price)
        # but BTC hasn't moved much
        for side_idx, side_name, side_price in [
            (0, "up", up_price),
            (1, "down", down_price),
        ]:
            if side_price >= max_buy_price:
                continue  # not cheap enough

            # Expected: if token is at $0.25, BTC should have moved ~0.25%+ against it
            # If BTC moved less, the token is oversold
            opposite_price = down_price if side_name == "up" else up_price

            # The expensive side should match BTC direction
            # If UP is cheap ($0.25) and DOWN is expensive ($0.75),
            # BTC should be dropping. If BTC is flat -> UP is oversold.
            expected_btc_move = (opposite_price - 0.50) * 1.0  # rough estimate

            # Check BTC movement since market start (using WS feed)
            try:
                btc_start_resp = httpx.get(
                    "https://api.binance.com/api/v3/klines",
                    params={
                        "symbol": "BTCUSDT",
                        "interval": "1m",
                        "startTime": ts * 1000,
                        "limit": 1,
                    },
                    timeout=5,
                )
                btc_start_resp.raise_for_status()
                btc_start_data = btc_start_resp.json()
                if btc_start_data:
                    btc_at_start = float(btc_start_data[0][1])
                else:
                    continue
            except Exception:
                continue

            btc_move = ((btc_now - btc_at_start) / btc_at_start) * 100

            # Flash crash detection logic:
            # If token is very cheap BUT BTC hasn't moved much in the "right" direction
            if side_name == "up" and side_price < 0.30:
                # UP is cheap -> BTC should be dropping significantly
                # If BTC is flat or up -> UP is oversold
                if btc_move > -max_btc_move_pct:
                    # BTC didn't drop much, but UP crashed -> anomaly!
                    drop_pct = (0.50 - side_price) / 0.50 * 100  # drop from 50/50
                    if drop_pct >= min_token_drop_pct:
                        signals.append(FlashCrashSignal(
                            slug=slug,
                            direction="up",
                            token_id=token_ids[0],
                            current_price=side_price,
                            previous_price=0.50,
                            price_drop_pct=drop_pct,
                            btc_move_pct=btc_move,
                            reason=(
                                f"FLASH CRASH: UP @ ${side_price:.2f} (dropped {drop_pct:.0f}%) "
                                f"but BTC only {btc_move:+.3f}% ({elapsed}s into market)"
                            ),
                        ))

            elif side_name == "down" and side_price < 0.30:
                # DOWN is cheap -> BTC should be rising significantly
                if btc_move < max_btc_move_pct:
                    drop_pct = (0.50 - side_price) / 0.50 * 100
                    if drop_pct >= min_token_drop_pct:
                        signals.append(FlashCrashSignal(
                            slug=slug,
                            direction="down",
                            token_id=token_ids[1],
                            current_price=side_price,
                            previous_price=0.50,
                            price_drop_pct=drop_pct,
                            btc_move_pct=btc_move,
                            reason=(
                                f"FLASH CRASH: DOWN @ ${side_price:.2f} (dropped {drop_pct:.0f}%) "
                                f"but BTC only {btc_move:+.3f}% ({elapsed}s into market)"
                            ),
                        ))

    return signals
