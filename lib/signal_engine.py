"""
Trade signal generation engine.

Combines three signal sources with weighted fusion:
  - Momentum signal (weight 0.40): BTC price trend vs market pricing
  - Orderbook imbalance signal (weight 0.45): CLOB bid/ask depth ratio
  - Sentiment filter (weight 0.15): Fear & Greed dampening

Inspired by the Polymarket-BTC-15-Minute-Trading-Bot reference implementation
which uses 6 signal processors with similar weighted fusion.
"""

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("polybmicob.signal")

# ── Polymarket fee calculation ────────────────────────────────
# Crypto markets: feeRate=0.25, exponent=2
# fee = shares * price * feeRate * (price * (1 - price))^exponent
POLY_FEE_RATE = 0.25
POLY_FEE_EXPONENT = 2


def calculate_poly_fee(shares: float, price: float) -> float:
    """
    Calculate Polymarket taker fee for crypto markets.

    Fee is dynamic: highest at p=0.50 (1.56%), lowest near 0 or 1 (0%).
    Formula: shares * price * feeRate * (price * (1-price))^exponent
    """
    if price <= 0 or price >= 1:
        return 0.0
    return shares * price * POLY_FEE_RATE * (price * (1 - price)) ** POLY_FEE_EXPONENT


def calculate_poly_fee_rate(price: float) -> float:
    """Return effective fee rate (%) for a given token price."""
    if price <= 0 or price >= 1:
        return 0.0
    return POLY_FEE_RATE * (price * (1 - price)) ** POLY_FEE_EXPONENT


def kelly_fraction(
    win_prob: float,
    entry_price: float,
    kelly_multiplier: float = 0.25,
) -> float:
    """
    Calculate optimal bet fraction using Fractional Kelly Criterion.

    For binary outcomes on Polymarket:
      b = (1 - entry_price) / entry_price  (net odds)
      f* = (b*p - q) / b
      Fractional Kelly = f* * kelly_multiplier

    Args:
        win_prob: Estimated probability of winning (0-1).
        entry_price: Token price (cost per share, 0-1).
        kelly_multiplier: Fraction of full Kelly (0.25 = Quarter-Kelly).

    Returns:
        Fraction of bankroll to bet (0.0 to 1.0, clamped).
    """
    if entry_price <= 0 or entry_price >= 1 or win_prob <= 0:
        return 0.0

    b = (1.0 - entry_price) / entry_price  # net odds
    q = 1.0 - win_prob
    full_kelly = (b * win_prob - q) / b

    if full_kelly <= 0:
        return 0.0  # negative Kelly = don't bet

    return min(full_kelly * kelly_multiplier, 0.25)  # cap at 25% of bankroll


# Signal weights (must sum to 1.0)
WEIGHT_MOMENTUM = 0.40
WEIGHT_ORDERBOOK = 0.45
WEIGHT_SENTIMENT = 0.15

# Thresholds
DEFAULT_MIN_EDGE = 0.10  # 10% minimum edge to trade
MOMENTUM_TREND_THRESHOLD = 0.05  # % change to count as a trend
MAX_PROBABILITY_ADJUSTMENT = 0.25  # cap probability shift at 25%


@dataclass
class TradeSignal:
    """A trade signal with direction, confidence, and reasoning."""

    market_slug: str
    direction: str  # "up" or "down"
    token_id: str  # the token to buy
    entry_price: float  # expected entry price
    edge: float  # estimated edge (0.0 to 1.0)
    confidence: float  # signal confidence (0.0 to 1.0)
    reason: str  # human-readable explanation


@dataclass
class OrderbookSignal:
    """Orderbook imbalance signal data."""

    direction: str  # "up", "down", or "neutral"
    strength: float  # 0.0 to 1.0
    bid_depth: float  # total bid depth in USD
    ask_depth: float  # total ask depth in USD
    imbalance: float  # -1.0 to 1.0


def compute_orderbook_imbalance(
    up_bids: list,
    up_asks: list,
    down_bids: list,
    down_asks: list,
) -> OrderbookSignal:
    """
    Compute orderbook imbalance for Up/Down tokens.

    Positive imbalance on Up token (more bids than asks) suggests
    buying pressure on "Up" outcome.

    Args:
        up_bids: List of (price, size) tuples for Up token bids.
        up_asks: List of (price, size) tuples for Up token asks.
        down_bids: List of (price, size) tuples for Down token bids.
        down_asks: List of (price, size) tuples for Down token asks.

    Returns:
        OrderbookSignal with direction, strength, and imbalance.
    """
    up_bid_depth = sum(p * s for p, s in up_bids) if up_bids else 0.0
    up_ask_depth = sum(p * s for p, s in up_asks) if up_asks else 0.0
    down_bid_depth = sum(p * s for p, s in down_bids) if down_bids else 0.0
    down_ask_depth = sum(p * s for p, s in down_asks) if down_asks else 0.0

    total_bid = up_bid_depth + down_bid_depth
    total_ask = up_ask_depth + down_ask_depth

    if total_bid + total_ask == 0:
        return OrderbookSignal(
            direction="neutral",
            strength=0.0,
            bid_depth=0.0,
            ask_depth=0.0,
            imbalance=0.0,
        )

    # Net imbalance: positive = more buying on Up, negative = more buying on Down
    # Compare Up bid pressure vs Down bid pressure
    if up_bid_depth + down_bid_depth > 0:
        up_pressure = up_bid_depth / (up_bid_depth + down_bid_depth)
    else:
        up_pressure = 0.5

    # Imbalance from -1 to +1 (0 = balanced)
    imbalance = (up_pressure - 0.5) * 2

    # Strength: how strong is the imbalance (0 to 1)
    strength = min(abs(imbalance), 1.0)

    if imbalance > 0.1:
        direction = "up"
    elif imbalance < -0.1:
        direction = "down"
    else:
        direction = "neutral"

    return OrderbookSignal(
        direction=direction,
        strength=strength,
        bid_depth=total_bid,
        ask_depth=total_ask,
        imbalance=imbalance,
    )


def _momentum_probability(momentum_pct: float, trend: str) -> tuple[float, float]:
    """
    Estimate true Up/Down probability from BTC price momentum.

    Base: 50/50. Adjusted by momentum strength.
    Momentum of +0.1% -> ~55% chance of Up
    Momentum of +0.3% -> ~65% chance of Up
    Capped at 75% (never be too confident on 5-min windows).

    Returns:
        (est_up_prob, est_down_prob)
    """
    if trend == "flat":
        return 0.50, 0.50

    # Scale: 0.1% momentum -> 5% probability shift, capped at 25%
    adjustment = min(abs(momentum_pct) * 50, MAX_PROBABILITY_ADJUSTMENT * 100) / 100

    if trend == "up":
        est_up = 0.50 + adjustment
        return est_up, 1.0 - est_up
    else:
        est_down = 0.50 + adjustment
        return 1.0 - est_down, est_down


def _sentiment_dampening(fear_greed_value: Optional[int]) -> float:
    """
    Compute a sentiment dampening factor.

    Extreme Fear (0-25): dampen confidence by 10-25% (markets are volatile)
    Fear (25-45): dampen by 5-10%
    Neutral (45-55): no dampening
    Greed (55-75): slight boost 5%
    Extreme Greed (75-100): boost 5-10% (strong trends more likely)

    Returns:
        Multiplier (0.75 to 1.10) applied to final confidence.
    """
    if fear_greed_value is None:
        return 1.0  # no data, no adjustment

    if fear_greed_value <= 25:
        # Extreme fear: dampen 10-25%
        return 0.75 + (fear_greed_value / 25) * 0.15
    elif fear_greed_value <= 45:
        # Fear: dampen 5-10%
        return 0.90 + ((fear_greed_value - 25) / 20) * 0.05
    elif fear_greed_value <= 55:
        # Neutral
        return 1.0
    elif fear_greed_value <= 75:
        # Greed: slight boost
        return 1.0 + ((fear_greed_value - 55) / 20) * 0.05
    else:
        # Extreme greed: boost 5-10%
        return 1.05 + ((fear_greed_value - 75) / 25) * 0.05


def generate_signal(
    momentum_pct: float,
    trend: str,
    up_price: Optional[float],
    down_price: Optional[float],
    up_token_id: str,
    down_token_id: str,
    market_slug: str,
    orderbook: Optional[OrderbookSignal] = None,
    fear_greed_value: Optional[int] = None,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> Optional[TradeSignal]:
    """
    Generate a trade signal using weighted signal fusion.

    Three signal sources:
      1. Momentum (40%): BTC price direction vs market pricing
      2. Orderbook (45%): Bid/ask depth imbalance on CLOB
      3. Sentiment (15%): Fear & Greed dampening factor

    Args:
        momentum_pct: BTC 5-minute momentum in percent.
        trend: "up", "down", or "flat".
        up_price: Current Up token price (None if no market data).
        down_price: Current Down token price (None if no market data).
        up_token_id: Token ID for "Up" outcome.
        down_token_id: Token ID for "Down" outcome.
        market_slug: Market slug for logging.
        orderbook: Optional orderbook imbalance signal.
        fear_greed_value: Optional Fear & Greed index (0-100).
        min_edge: Minimum edge threshold (default 10%).

    Returns:
        TradeSignal if edge >= min_edge, else None.
    """
    if up_price is None or down_price is None:
        return None

    # Sanity: prices must be in valid range
    if not (0.01 <= up_price <= 0.99) or not (0.01 <= down_price <= 0.99):
        return None

    # --- Signal 1: Momentum ---
    mom_up_prob, mom_down_prob = _momentum_probability(momentum_pct, trend)

    # --- Signal 2: Orderbook ---
    if orderbook is not None and orderbook.direction != "neutral":
        # Orderbook shifts probability estimate
        ob_adjustment = orderbook.strength * 0.15  # max 15% shift from orderbook
        if orderbook.direction == "up":
            ob_up_prob = 0.50 + ob_adjustment
            ob_down_prob = 1.0 - ob_up_prob
        else:
            ob_down_prob = 0.50 + ob_adjustment
            ob_up_prob = 1.0 - ob_down_prob
    else:
        ob_up_prob = 0.50
        ob_down_prob = 0.50

    # --- Weighted fusion ---
    if orderbook is not None:
        fused_up = (
            WEIGHT_MOMENTUM * mom_up_prob
            + WEIGHT_ORDERBOOK * ob_up_prob
            + WEIGHT_SENTIMENT * 0.50  # sentiment is a multiplier, not a probability
        )
        fused_down = 1.0 - fused_up
    else:
        # No orderbook data: redistribute weight to momentum
        fused_up = mom_up_prob
        fused_down = mom_down_prob

    # --- Signal 3: Sentiment dampening ---
    sentiment_mult = _sentiment_dampening(fear_greed_value)
    # Sentiment amplifies or dampens our deviation from 50/50
    deviation_up = fused_up - 0.50
    fused_up = 0.50 + deviation_up * sentiment_mult
    fused_down = 1.0 - fused_up

    # Clamp probabilities to [0.05, 0.95]
    fused_up = max(0.05, min(0.95, fused_up))
    fused_down = max(0.05, min(0.95, fused_down))

    # --- Edge calculation ---
    up_edge = fused_up - up_price
    down_edge = fused_down - down_price

    # Build reason parts
    reason_parts = [f"momentum={momentum_pct:+.3f}%"]
    if orderbook is not None:
        reason_parts.append(
            f"ob_imbalance={orderbook.imbalance:+.2f}"
        )
    if fear_greed_value is not None:
        reason_parts.append(f"F&G={fear_greed_value}")

    # Pick the direction with better edge
    if up_edge > down_edge and up_edge >= min_edge:
        reason = (
            f"{' '.join(reason_parts)} -> "
            f"est {fused_up:.0%} Up vs market {up_price:.0%}"
        )
        return TradeSignal(
            market_slug=market_slug,
            direction="up",
            token_id=up_token_id,
            entry_price=up_price,
            edge=up_edge,
            confidence=fused_up,
            reason=reason,
        )
    elif down_edge >= min_edge:
        reason = (
            f"{' '.join(reason_parts)} -> "
            f"est {fused_down:.0%} Down vs market {down_price:.0%}"
        )
        return TradeSignal(
            market_slug=market_slug,
            direction="down",
            token_id=down_token_id,
            entry_price=down_price,
            edge=down_edge,
            confidence=fused_down,
            reason=reason,
        )

    return None  # no sufficient edge
