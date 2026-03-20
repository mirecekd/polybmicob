"""
Black-Scholes fair value for BTC 5-minute binary options.

Computes the theoretical probability that BTC will be higher/lower at the
end of a 5-minute window using the Black-Scholes framework for digital options.

For a binary option "BTC up in T seconds":
  P(up) = N(d2)
  d2 = (mu * T_years) / (sigma * sqrt(T_years))

Where:
  mu    = annualized drift (from recent momentum)
  sigma = annualized realized volatility (from recent 1m klines)
  T     = time remaining in years (e.g. 300s / 31536000)
  N()   = standard normal CDF

For in-play markets with an existing BTC move:
  d2 = (current_move_pct / 100 + mu * T_remaining) / (sigma * sqrt(T_remaining))

This replaces heuristic confidence estimates with a mathematically grounded model.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("polybmicob.bs")

# Constants
SECONDS_PER_YEAR = 365.25 * 24 * 3600  # 31,557,600
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Volatility cache (recomputed every N seconds)
_vol_cache: Optional[float] = None
_vol_cache_time: float = 0.0
_VOL_CACHE_TTL = 60.0  # refresh every 60 seconds


def _normal_cdf(x: float) -> float:
    """
    Standard normal CDF approximation using the error function.

    Accurate to ~1e-7. No external dependencies needed.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class BSFairValue:
    """Black-Scholes fair value for a BTC 5-minute binary option."""

    prob_up: float  # probability BTC ends higher (0-1)
    prob_down: float  # probability BTC ends lower (0-1)
    sigma_annual: float  # annualized volatility used
    sigma_5m: float  # 5-minute volatility (sigma * sqrt(T_5m))
    d2: float  # the d2 value from BS formula
    drift_annual: float  # annualized drift used


def compute_realized_volatility(
    num_candles: int = 30,
    interval: str = "1m",
) -> Optional[float]:
    """
    Compute annualized realized volatility from recent Binance klines.

    Uses log returns of 1-minute close prices.
    30 x 1m candles = 30 minutes of data -> reasonable short-term vol estimate.

    Args:
        num_candles: Number of 1m candles to use (default 30 = 30 minutes).
        interval: Kline interval (default "1m").

    Returns:
        Annualized volatility as a decimal (e.g. 0.50 = 50% annual vol),
        or None on error.
    """
    import time
    global _vol_cache, _vol_cache_time

    now = time.time()
    if _vol_cache is not None and (now - _vol_cache_time) < _VOL_CACHE_TTL:
        return _vol_cache

    try:
        resp = httpx.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": "BTCUSDT",
                "interval": interval,
                "limit": num_candles + 1,  # need N+1 prices for N returns
            },
            timeout=10,
        )
        resp.raise_for_status()
        klines = resp.json()

        if len(klines) < 3:
            return None

        # Extract close prices
        closes = [float(k[4]) for k in klines]

        # Compute log returns
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                lr = math.log(closes[i] / closes[i - 1])
                log_returns.append(lr)

        if len(log_returns) < 2:
            return None

        # Standard deviation of log returns
        mean_lr = sum(log_returns) / len(log_returns)
        variance = sum((lr - mean_lr) ** 2 for lr in log_returns) / (len(log_returns) - 1)
        std_1m = math.sqrt(variance)

        # Annualize: multiply by sqrt(minutes_per_year)
        # 1 minute interval -> 525,960 minutes per year
        minutes_per_year = SECONDS_PER_YEAR / 60
        sigma_annual = std_1m * math.sqrt(minutes_per_year)

        _vol_cache = sigma_annual
        _vol_cache_time = now

        log.debug(
            "Realized vol: std_1m=%.6f%%, sigma_annual=%.1f%%, from %d returns",
            std_1m * 100, sigma_annual * 100, len(log_returns),
        )

        return sigma_annual

    except Exception as exc:
        log.debug("Failed to compute realized volatility: %s", exc)
        if _vol_cache is not None:
            return _vol_cache  # return stale cache on error
        return None


def bs_fair_value(
    momentum_pct: float,
    time_remaining_sec: float = 300.0,
    sigma_annual: Optional[float] = None,
) -> Optional[BSFairValue]:
    """
    Compute Black-Scholes fair value for a BTC 5-minute binary option.

    For pre-market: time_remaining = 300s, momentum = recent BTC trend.
    For in-play: time_remaining = seconds left, momentum = actual move since start.

    The key insight: at a given volatility, we can calculate the mathematical
    probability that BTC continues in the current direction for the remaining time.

    Args:
        momentum_pct: Current BTC momentum in percent (e.g. +0.10 for +0.10%).
            For pre-market: 5-minute momentum from klines.
            For in-play: actual BTC move since market start.
        time_remaining_sec: Seconds remaining in the 5-minute window.
        sigma_annual: Annualized volatility (if None, will be fetched).

    Returns:
        BSFairValue with prob_up, prob_down, and model parameters.
        None on error.
    """
    if time_remaining_sec <= 0:
        # Market ended - 100% probability to whichever direction
        if momentum_pct > 0:
            return BSFairValue(1.0, 0.0, 0.0, 0.0, 999.0, 0.0)
        elif momentum_pct < 0:
            return BSFairValue(0.0, 1.0, 0.0, 0.0, -999.0, 0.0)
        else:
            return BSFairValue(0.5, 0.5, 0.0, 0.0, 0.0, 0.0)

    # Get volatility
    if sigma_annual is None:
        sigma_annual = compute_realized_volatility()
    if sigma_annual is None or sigma_annual <= 0:
        return None

    # Convert time to years
    t_years = time_remaining_sec / SECONDS_PER_YEAR

    # Convert momentum to annualized drift
    # momentum_pct is the % move over the observation period
    # For pre-market: this is ~5min momentum, annualize it
    # For in-play: this is actual move, use as drift for remaining time
    #
    # Drift = (momentum_pct / 100) / t_observation, annualized
    # But for simplicity, we treat momentum as the "current state" and
    # compute P(BTC stays in this direction for remaining time)
    #
    # d2 = (current_advantage + expected_drift) / uncertainty
    # current_advantage = momentum_pct / 100 (already realized move)
    # expected_drift = 0 (assume no further drift, conservative)
    # uncertainty = sigma * sqrt(T_remaining)

    sigma_t = sigma_annual * math.sqrt(t_years)

    if sigma_t <= 0:
        return None

    # For binary option: P(S_T > S_0) where S is already at S_0 * (1 + momentum)
    # If we're measuring "will BTC end higher than at market start":
    # The current advantage is momentum_pct/100, noise is sigma*sqrt(T_remaining)
    #
    # d2 = current_move / future_uncertainty
    # This captures: "given BTC already moved +0.1%, what's the probability
    # it won't reverse by more than 0.1% in the remaining time?"

    current_move = momentum_pct / 100.0  # convert percent to decimal
    d2 = current_move / sigma_t

    prob_up = _normal_cdf(d2)
    prob_down = 1.0 - prob_up

    # Clamp to reasonable range (never 0% or 100%)
    prob_up = max(0.02, min(0.98, prob_up))
    prob_down = max(0.02, min(0.98, prob_down))

    # 5-minute volatility for logging
    t_5m = 300.0 / SECONDS_PER_YEAR
    sigma_5m = sigma_annual * math.sqrt(t_5m)

    return BSFairValue(
        prob_up=prob_up,
        prob_down=prob_down,
        sigma_annual=sigma_annual,
        sigma_5m=sigma_5m,
        d2=d2,
        drift_annual=0.0,  # we use zero drift (conservative)
    )
