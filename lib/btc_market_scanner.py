"""
BTC 5-minute Up/Down market scanner.

Discovers upcoming btc-updown-5m-* markets via Gamma API.
Each market is a binary prediction: will BTC go Up or Down in a 5-minute window?

Slug format: btc-updown-5m-{UNIX_TIMESTAMP}
  - Timestamp = START of the 5-minute window (UTC)
  - Window end = timestamp + 300 seconds

Token mapping:
  - clobTokenIds[0] = "Up" outcome
  - clobTokenIds[1] = "Down" outcome

Discovery strategy:
  The Gamma API tag=crypto filter does NOT return these markets reliably.
  Instead, we generate expected slugs for upcoming 5-min windows and query
  each by exact slug. Markets are on a 300-second grid.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("polybmicob.scanner")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class BtcMarket:
    """A single BTC 5-minute Up/Down market."""

    event_id: str
    market_id: str
    slug: str
    question: str
    condition_id: str
    up_token_id: str  # clobTokenIds[0]
    down_token_id: str  # clobTokenIds[1]
    up_price: Optional[float]
    down_price: Optional[float]
    volume: float
    liquidity: float
    start_time: datetime  # window start (from slug timestamp)
    end_time: datetime  # window end (start + 300s)
    minutes_to_start: float
    minutes_to_end: float


def _parse_json_field(raw) -> Optional[list]:
    """Parse a Gamma API field that may be None, a JSON string, or a list."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(raw, list):
        return raw
    return None


def _parse_event(event: dict, now: datetime) -> Optional[BtcMarket]:
    """Parse a Gamma API event into a BtcMarket, or None if invalid."""
    slug = event.get("slug", "")
    if not slug.startswith("btc-updown-5m-"):
        return None

    try:
        start_ts = int(slug.split("-")[-1])
    except (ValueError, IndexError):
        return None

    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(start_ts + 300, tz=timezone.utc)

    mins_to_start = (start_dt - now).total_seconds() / 60
    mins_to_end = (end_dt - now).total_seconds() / 60

    # Each 5m event has exactly 1 market
    event_markets = event.get("markets", [])
    if not event_markets:
        return None
    mkt = event_markets[0]

    # Parse token IDs
    token_ids = _parse_json_field(mkt.get("clobTokenIds"))
    if not token_ids or len(token_ids) < 2:
        return None

    # Parse outcome prices (can be None for markets with no activity)
    prices = _parse_json_field(mkt.get("outcomePrices"))
    up_price = None
    down_price = None
    if prices and len(prices) >= 2:
        try:
            up_price = float(prices[0])
            down_price = float(prices[1])
        except (ValueError, TypeError):
            pass

    return BtcMarket(
        event_id=str(event.get("id", "")),
        market_id=str(mkt.get("id", "")),
        slug=slug,
        question=mkt.get("question", ""),
        condition_id=mkt.get("conditionId", ""),
        up_token_id=token_ids[0],
        down_token_id=token_ids[1],
        up_price=up_price,
        down_price=down_price,
        volume=float(mkt.get("volume", 0) or 0),
        liquidity=float(mkt.get("liquidity", 0) or 0),
        start_time=start_dt,
        end_time=end_dt,
        minutes_to_start=mins_to_start,
        minutes_to_end=mins_to_end,
    )


def _generate_upcoming_slugs(
    min_minutes: float,
    max_minutes: float,
) -> list[str]:
    """Generate expected btc-updown-5m slugs for the given time window."""
    now_ts = int(time.time())
    min_ts = now_ts + int(min_minutes * 60)
    max_ts = now_ts + int(max_minutes * 60)

    # Align to 300-second grid
    first_ts = min_ts - (min_ts % 300)
    if first_ts < min_ts:
        first_ts += 300

    slugs = []
    ts = first_ts
    while ts <= max_ts:
        slugs.append(f"btc-updown-5m-{ts}")
        ts += 300

    return slugs


def scan_btc_5m_markets(
    min_minutes: float = 1.0,
    max_minutes: float = 10.0,
) -> list[BtcMarket]:
    """
    Find BTC 5m Up/Down markets starting within the given time window.

    Generates expected slugs and queries Gamma API for each.
    Markets are on a 300-second grid.

    Args:
        min_minutes: Minimum minutes until market start (default 1.0).
        max_minutes: Maximum minutes until market start (default 10.0).

    Returns:
        List of BtcMarket sorted by start_time (soonest first).
    """
    now = datetime.now(timezone.utc)
    slugs = _generate_upcoming_slugs(min_minutes, max_minutes)

    if not slugs:
        return []

    log.debug("Checking %d slugs: %s ... %s", len(slugs), slugs[0], slugs[-1])

    markets: list[BtcMarket] = []

    for slug in slugs:
        try:
            resp = httpx.get(
                f"{GAMMA_API_BASE}/events",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()

            if not events:
                continue

            parsed = _parse_event(events[0], now)
            if parsed is not None:
                markets.append(parsed)

        except Exception as exc:
            log.warning("Failed to fetch slug %s: %s", slug, exc)
            continue

    markets.sort(key=lambda m: m.start_time)
    return markets
