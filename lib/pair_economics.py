"""
Pair Economics Engine - Fee-adjusted economic analysis for MM pair lifecycle.

Pure functions, no side effects, no imports from scripts/.
Computes fee-adjusted economics for:
  1. Entry: Should we quote both sides on this market?
  2. Completion: Should we buy the missing second leg?
  3. Exit: Should we quick-flip or hold to resolution?

Polymarket crypto fee model:
  - Taker fee: feeRate * (price * (1-price))^2, max ~1.56% at p=0.50
  - Maker fee: $0 (+ optional rebate from 20% pool)
"""

from dataclasses import dataclass
from typing import Optional


# ---- Dataclasses --------------------------------------------------------


@dataclass
class PairLegQuote:
    """Orderbook snapshot for one side (UP or DOWN)."""

    best_bid: Optional[float]  # Best bid price (None if no bids)
    best_ask: Optional[float]  # Best ask price (None if no asks)
    bid_depth_usd: float  # Total bid depth in USD (top N levels)
    ask_depth_usd: float  # Total ask depth in USD (top N levels)


@dataclass
class PairEntryAnalysis:
    """Result of analyzing whether to enter an MM pair."""

    # Costs
    pair_cost_at_bid: Optional[float]  # Cost if both fill as maker at best_bid
    pair_cost_at_ask: Optional[float]  # Cost if both taken at best_ask (worst case)

    # Profits (per share)
    gross_locked_profit: Optional[float]  # 1.0 - pair_cost (before fees)
    fee_adjusted_locked_profit: Optional[float]  # After maker rebate / fees

    # Completion estimate (if only one side fills)
    expected_completion_cost: Optional[float]  # filled_price + opposite_ask + slippage
    expected_completion_profit: Optional[float]  # 1.0 - expected_completion_cost - fees

    # Risk
    partial_fill_risk: float  # 0.0 (safe) to 1.0 (dangerous)

    # Classification
    classification: str  # "STRICT_ARB", "GOOD_MM", "WEAK_MM", "NO_EDGE"
    is_viable: bool  # Should we enter?
    reason: str  # Human-readable explanation


@dataclass
class PairCompletionAnalysis:
    """Result of analyzing whether to complete (buy missing leg)."""

    pair_cost: float  # filled_price + opposite_price
    gross_profit: Optional[float]
    fee_adjusted_profit: Optional[float]
    should_complete: bool
    reason: str


@dataclass
class PairExitAnalysis:
    """Result of analyzing whether to quick-flip or hold."""

    # Current liquidation value
    liquidation_value: Optional[float]  # best_bid_up + best_bid_down

    # Quick flip economics
    gross_flip_profit: Optional[float]  # liquidation - pair_cost
    fee_adjusted_flip_profit: Optional[float]  # After taker sell fees

    # Comparison
    hold_to_resolution_value: float  # Always 1.00 (one side wins)
    hold_profit: float  # 1.00 - pair_cost (minus claim cost ~$0)

    # Decision
    should_quick_flip: bool
    reason: str


# ---- Fee calculation ----------------------------------------------------


def calculate_taker_fee_rate(price: float) -> float:
    """
    Polymarket taker fee rate for crypto markets.

    Fee is dynamic: highest at p=0.50 (~1.56%), approaches 0% near 0 or 1.
    Formula: feeRate * (price * (1 - price))^exponent
    where feeRate=0.25, exponent=2.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


def calculate_taker_fee(price: float, shares: float) -> float:
    """Absolute taker fee for a given price and share count."""
    return shares * price * calculate_taker_fee_rate(price)


# ---- Partial fill risk --------------------------------------------------


def _compute_partial_risk(
    up: PairLegQuote,
    down: PairLegQuote,
    depth_safety_ratio: float,
) -> float:
    """
    Score 0.0 (safe) to 1.0 (dangerous).

    High risk when:
    - One side has much less depth than the other (asymmetric fill probability)
    - Total depth is very low (thin market)
    - Spread is wide on one side (slippage risk)
    """
    up_depth = up.bid_depth_usd * depth_safety_ratio
    down_depth = down.bid_depth_usd * depth_safety_ratio

    if up_depth + down_depth < 1.0:
        return 1.0  # No meaningful depth

    # Depth asymmetry: ratio of smaller to larger
    min_depth = min(up_depth, down_depth)
    max_depth = max(up_depth, down_depth)
    asymmetry = 1.0 - (min_depth / max_depth) if max_depth > 0 else 1.0

    # Spread risk: wide spread on either side
    up_spread = (
        (up.best_ask - up.best_bid)
        if up.best_bid is not None and up.best_ask is not None
        else 0.10
    )
    down_spread = (
        (down.best_ask - down.best_bid)
        if down.best_bid is not None and down.best_ask is not None
        else 0.10
    )
    spread_risk = min((up_spread + down_spread) / 0.10, 1.0)  # normalized

    return min(asymmetry * 0.6 + spread_risk * 0.4, 1.0)


# ---- Core analysis functions --------------------------------------------


def analyze_pair_entry(
    up: PairLegQuote,
    down: PairLegQuote,
    trade_size_usd: float,
    min_profit_per_share: float = 0.01,
    max_pair_cost: float = 0.98,
    maker_rebate_rate: float = 0.0,
    completion_slippage: float = 0.01,
    depth_safety_ratio: float = 0.25,
) -> PairEntryAnalysis:
    """
    Analyze whether entering an MM pair is economically viable.

    Logic:
    1. Compute pair_cost at bid (maker) and ask (taker)
    2. Compute fee-adjusted locked profit if both sides fill
    3. Estimate completion cost if only one side fills
    4. Score partial fill risk based on depth asymmetry
    5. Classify: STRICT_ARB / GOOD_MM / WEAK_MM / NO_EDGE

    Classification rules:
    - STRICT_ARB: pair_cost_at_ask < 1.0 - min_profit (guaranteed profit at market)
    - GOOD_MM: pair_cost_at_bid < max_pair_cost AND profit > min_profit AND risk OK
    - WEAK_MM: pair_cost_at_bid < 1.0 but profit marginal or risk too high
    - NO_EDGE: pair_cost_at_bid >= 1.0 or missing quotes
    """
    # Validate quotes
    if up.best_bid is None or down.best_bid is None:
        return PairEntryAnalysis(
            pair_cost_at_bid=None,
            pair_cost_at_ask=None,
            gross_locked_profit=None,
            fee_adjusted_locked_profit=None,
            expected_completion_cost=None,
            expected_completion_profit=None,
            partial_fill_risk=1.0,
            classification="NO_EDGE",
            is_viable=False,
            reason="Missing bids on one or both sides",
        )

    # 1. Pair costs
    pair_cost_at_bid = up.best_bid + down.best_bid

    pair_cost_at_ask: Optional[float] = None
    if up.best_ask is not None and down.best_ask is not None:
        pair_cost_at_ask = up.best_ask + down.best_ask

    # 2. Locked profit (both sides fill as maker = $0 fee)
    gross_locked = 1.0 - pair_cost_at_bid
    # Maker rebate: earn rebate on each side
    rebate_per_share = maker_rebate_rate * pair_cost_at_bid
    fee_adjusted_locked = gross_locked + rebate_per_share

    # 3. Completion estimate (one side fills as maker, other as taker FOK)
    expected_completion_cost: Optional[float] = None
    expected_completion_profit: Optional[float] = None

    # Average scenario: our bid fills on cheaper side, we complete at ask + slippage
    # on the more expensive side
    if up.best_ask is not None and down.best_ask is not None:
        # Worst-case completion: we fill at our bid, complete at opposite ask + slippage
        up_completion = up.best_bid + (down.best_ask + completion_slippage)
        down_completion = down.best_bid + (up.best_ask + completion_slippage)
        # Use the better scenario (cheaper completion)
        expected_completion_cost = min(up_completion, down_completion)
        # Taker fee on the completion side
        if expected_completion_cost == up_completion:
            completion_fee = calculate_taker_fee_rate(down.best_ask + completion_slippage)
        else:
            completion_fee = calculate_taker_fee_rate(up.best_ask + completion_slippage)
        expected_completion_profit = (
            1.0 - expected_completion_cost - completion_fee
        )

    # 4. Partial fill risk
    partial_risk = _compute_partial_risk(up, down, depth_safety_ratio)

    # 5. Classification
    classification: str
    is_viable: bool
    reason: str

    # STRICT_ARB: can take both sides at ask prices for guaranteed profit
    if pair_cost_at_ask is not None and pair_cost_at_ask < (1.0 - min_profit_per_share):
        classification = "STRICT_ARB"
        is_viable = True
        arb_profit = 1.0 - pair_cost_at_ask
        reason = (
            f"Strict arb: ask_cost=${pair_cost_at_ask:.3f}, "
            f"guaranteed=${arb_profit:.4f}/share"
        )

    # NO_EDGE: bid cost >= 1.0 (no possible profit)
    elif pair_cost_at_bid >= 1.0:
        classification = "NO_EDGE"
        is_viable = False
        reason = f"No edge: bid_cost=${pair_cost_at_bid:.3f} >= $1.00"

    # GOOD_MM: good economics + acceptable risk
    elif (
        pair_cost_at_bid <= max_pair_cost
        and fee_adjusted_locked > min_profit_per_share
        and partial_risk < 0.7
    ):
        classification = "GOOD_MM"
        is_viable = True
        reason = (
            f"Good MM: bid_cost=${pair_cost_at_bid:.3f}, "
            f"fee_adj_profit=${fee_adjusted_locked:.4f}/share, "
            f"risk={partial_risk:.2f}"
        )

    # WEAK_MM: technically profitable but marginal or risky
    elif pair_cost_at_bid < 1.0:
        classification = "WEAK_MM"
        is_viable = False
        reasons = []
        if pair_cost_at_bid > max_pair_cost:
            reasons.append(
                f"cost=${pair_cost_at_bid:.3f}>${max_pair_cost:.2f}"
            )
        if fee_adjusted_locked <= min_profit_per_share:
            reasons.append(
                f"profit=${fee_adjusted_locked:.4f}<=${min_profit_per_share}"
            )
        if partial_risk >= 0.7:
            reasons.append(f"risk={partial_risk:.2f}>=0.70")
        reason = f"Weak MM: {', '.join(reasons)}"

    else:
        classification = "NO_EDGE"
        is_viable = False
        reason = f"No edge: bid_cost=${pair_cost_at_bid:.3f}"

    return PairEntryAnalysis(
        pair_cost_at_bid=pair_cost_at_bid,
        pair_cost_at_ask=pair_cost_at_ask,
        gross_locked_profit=gross_locked,
        fee_adjusted_locked_profit=fee_adjusted_locked,
        expected_completion_cost=expected_completion_cost,
        expected_completion_profit=expected_completion_profit,
        partial_fill_risk=partial_risk,
        classification=classification,
        is_viable=is_viable,
        reason=reason,
    )


def analyze_pair_completion(
    filled_price: float,
    filled_direction: str,
    opposite_best_ask: Optional[float],
    min_profit_per_share: float = 0.01,
    max_pair_cost: float = 1.00,
    slippage_buffer: float = 0.01,
) -> PairCompletionAnalysis:
    """
    Analyze whether to complete a partial MM pair by buying the opposite side.

    Logic:
    1. pair_cost = filled_price + opposite_ask + slippage
    2. Completion is FOK taker order -> include taker fee on opposite side
    3. profit = 1.0 - pair_cost - taker_fee
    4. should_complete only if profit > min_profit AND pair_cost <= max_pair_cost
    """
    if opposite_best_ask is None:
        return PairCompletionAnalysis(
            pair_cost=filled_price,
            gross_profit=None,
            fee_adjusted_profit=None,
            should_complete=False,
            reason="No asks on opposite side",
        )

    completion_price = opposite_best_ask + slippage_buffer
    pair_cost = filled_price + completion_price

    gross_profit = 1.0 - pair_cost
    taker_fee = calculate_taker_fee_rate(completion_price)
    fee_adjusted_profit = gross_profit - taker_fee

    if pair_cost > max_pair_cost:
        return PairCompletionAnalysis(
            pair_cost=pair_cost,
            gross_profit=gross_profit,
            fee_adjusted_profit=fee_adjusted_profit,
            should_complete=False,
            reason=(
                f"Pair cost ${pair_cost:.3f} > ${max_pair_cost:.2f} max "
                f"(filled {filled_direction} @ ${filled_price:.2f}, "
                f"opp ask ${opposite_best_ask:.2f}+${slippage_buffer:.2f} slip)"
            ),
        )

    if fee_adjusted_profit < min_profit_per_share:
        return PairCompletionAnalysis(
            pair_cost=pair_cost,
            gross_profit=gross_profit,
            fee_adjusted_profit=fee_adjusted_profit,
            should_complete=False,
            reason=(
                f"Fee-adjusted profit ${fee_adjusted_profit:.4f} "
                f"< ${min_profit_per_share} min "
                f"(pair_cost=${pair_cost:.3f}, fee={taker_fee:.4f})"
            ),
        )

    return PairCompletionAnalysis(
        pair_cost=pair_cost,
        gross_profit=gross_profit,
        fee_adjusted_profit=fee_adjusted_profit,
        should_complete=True,
        reason=(
            f"Complete: pair_cost=${pair_cost:.3f}, "
            f"fee_adj_profit=${fee_adjusted_profit:.4f}/share "
            f"(filled {filled_direction} @ ${filled_price:.2f}, "
            f"opp ${completion_price:.2f} incl slip, fee={taker_fee:.4f})"
        ),
    )


def analyze_pair_exit(
    up_best_bid: Optional[float],
    down_best_bid: Optional[float],
    pair_cost: float,
    shares: int,
    min_profit_per_share: float = 0.01,
) -> PairExitAnalysis:
    """
    Analyze whether to quick-flip (sell both sides) or hold to resolution.

    Logic:
    1. liquidation_value = up_bid + down_bid
    2. sell_fees = taker_fee(up_bid) + taker_fee(down_bid) per share
    3. gross_flip_profit = liquidation_value - pair_cost
    4. fee_adjusted = gross_flip_profit - sell_fees
    5. Compare with hold_profit = 1.0 - pair_cost (no fees on resolution + claim)
    6. should_quick_flip if fee_adjusted > min_profit AND fee_adjusted > 0.5 * hold_profit
       (don't flip if holding is much more profitable)
    """
    hold_profit = 1.0 - pair_cost

    if up_best_bid is None or down_best_bid is None:
        return PairExitAnalysis(
            liquidation_value=None,
            gross_flip_profit=None,
            fee_adjusted_flip_profit=None,
            hold_to_resolution_value=1.0,
            hold_profit=hold_profit,
            should_quick_flip=False,
            reason="Missing bids on one or both sides",
        )

    liquidation_value = up_best_bid + down_best_bid

    # Sell fees (taker on each side)
    up_sell_fee = calculate_taker_fee_rate(up_best_bid)
    down_sell_fee = calculate_taker_fee_rate(down_best_bid)
    total_sell_fee_per_share = up_sell_fee + down_sell_fee

    gross_flip = liquidation_value - pair_cost
    fee_adjusted_flip = gross_flip - total_sell_fee_per_share

    # Decision: flip only if profitable AND at least 50% of hold-to-resolution profit
    # This prevents flipping at $0.02 when holding would give $0.05
    should_flip = (
        fee_adjusted_flip > min_profit_per_share
        and fee_adjusted_flip > 0.5 * hold_profit
    )

    if should_flip:
        reason = (
            f"Quick flip: liq=${liquidation_value:.3f}, "
            f"fee_adj=${fee_adjusted_flip:.4f}/share "
            f"vs hold=${hold_profit:.4f}/share "
            f"(fees={total_sell_fee_per_share:.4f})"
        )
    elif fee_adjusted_flip <= min_profit_per_share:
        reason = (
            f"Hold: flip profit ${fee_adjusted_flip:.4f} "
            f"< ${min_profit_per_share} min "
            f"(liq=${liquidation_value:.3f}, fees={total_sell_fee_per_share:.4f})"
        )
    else:
        reason = (
            f"Hold: flip ${fee_adjusted_flip:.4f} "
            f"< 50% of hold ${hold_profit:.4f} "
            f"(not worth flipping early)"
        )

    return PairExitAnalysis(
        liquidation_value=liquidation_value,
        gross_flip_profit=gross_flip,
        fee_adjusted_flip_profit=fee_adjusted_flip,
        hold_to_resolution_value=1.0,
        hold_profit=hold_profit,
        should_quick_flip=should_flip,
        reason=reason,
    )
