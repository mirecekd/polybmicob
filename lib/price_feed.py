"""
BTC price feed and momentum calculation.

Data sources:
  - Binance REST API (BTC/USDT price + 1m klines)
  - Alternative.me Fear & Greed Index (cached, refreshed every 30 min)
"""

import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("polybmicob.price_feed")

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

# Fear & Greed cache (refreshed every 30 minutes)
_fg_cache: Optional[dict] = None
_fg_cache_time: float = 0.0
_FG_CACHE_TTL = 1800  # 30 minutes


@dataclass
class PriceSnapshot:
    """BTC price with momentum data."""

    price: float
    timestamp: datetime
    momentum_5m: float  # percent change over last 5 minutes
    trend: str  # "up", "down", "flat"


@dataclass
class FearGreedData:
    """Fear & Greed Index data."""

    value: int  # 0-100 (0 = extreme fear, 100 = extreme greed)
    classification: str  # e.g. "Fear", "Greed", "Extreme Fear"
    timestamp: datetime


def get_btc_price() -> float:
    """Get current BTC/USDT price from Binance."""
    resp = httpx.get(
        BINANCE_TICKER_URL,
        params={"symbol": "BTCUSDT"},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["price"])


def get_btc_momentum() -> PriceSnapshot:
    """
    Get BTC price with 5-minute momentum from 1m klines.

    Uses 6 x 1-minute candles to calculate price change over ~5 minutes.
    Trend thresholds: >+0.05% = up, <-0.05% = down, else flat.
    """
    resp = httpx.get(
        BINANCE_KLINES_URL,
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 6},
        timeout=10,
    )
    resp.raise_for_status()
    klines = resp.json()

    price_now = float(klines[-1][4])  # latest close
    price_5m = float(klines[0][1])  # open of candle ~5 minutes ago

    if price_5m == 0:
        momentum = 0.0
    else:
        momentum = (price_now - price_5m) / price_5m * 100

    if momentum > 0.05:
        trend = "up"
    elif momentum < -0.05:
        trend = "down"
    else:
        trend = "flat"

    return PriceSnapshot(
        price=price_now,
        timestamp=datetime.now(timezone.utc),
        momentum_5m=momentum,
        trend=trend,
    )


def get_fear_greed_index() -> Optional[FearGreedData]:
    """
    Get Fear & Greed Index from alternative.me.

    Cached for 30 minutes (the index updates daily anyway).
    Returns None on error (non-critical data source).
    """
    global _fg_cache, _fg_cache_time

    now = time.time()
    if _fg_cache is not None and (now - _fg_cache_time) < _FG_CACHE_TTL:
        return _fg_cache

    try:
        resp = httpx.get(FEAR_GREED_URL, params={"limit": 1}, timeout=10)
        resp.raise_for_status()
        data = resp.json()["data"][0]
        result = FearGreedData(
            value=int(data["value"]),
            classification=data["value_classification"],
            timestamp=datetime.fromtimestamp(
                int(data["timestamp"]), tz=timezone.utc
            ),
        )
        _fg_cache = result
        _fg_cache_time = now
        log.info(
            "Fear & Greed Index: %d (%s)", result.value, result.classification
        )
        return result
    except Exception as exc:
        log.warning("Failed to fetch Fear & Greed Index: %s", exc)
        # Return cached value if available, even if stale
        if _fg_cache is not None:
            return _fg_cache
        return None
