#!/usr/bin/env python3
"""
Reversal Sniper Backtest for PolyBMiCoB

Simulates a "buy the cheap side" strategy on BTC 5-minute markets:
  - At t=120s into a 5min candle, check BTC direction since candle open
  - If BTC moved significantly in one direction, buy the OPPOSITE (cheap) side
  - Check if BTC reverses by candle close

The idea: when BTC drops 0.1%+ in first 2 minutes, DOWN token is ~$0.80+
and UP token is cheap ($0.05-0.20). If BTC reverses, UP pays $1.00.

Compares three strategies:
  A) Current: buy the momentum side (expensive token ~$0.70-0.85)
  B) Reversal sniper: buy the opposite side (cheap token ~$0.05-0.20)
  C) Hybrid: small momentum bet + larger reversal bet

Usage:
  python scripts/backtest_reversal.py                  # last 7 days
  python scripts/backtest_reversal.py --hours 168      # last 7 days
  python scripts/backtest_reversal.py --hours 720      # last 30 days
"""

import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx


def fetch_1m_klines(hours: int = 168) -> list[dict]:
    """Fetch 1-minute klines from Binance for granular in-play simulation."""
    print(f"Fetching {hours}h of BTC 1m klines from Binance...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (hours * 3600 * 1000)

    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        resp = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": current_start,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        for k in data:
            all_klines.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })

        current_start = data[-1][0] + 1
        time.sleep(0.2)
        print(f"  ...fetched {len(all_klines)} candles", end="\r")

    print(f"  Got {len(all_klines)} 1-minute candles")
    return all_klines


def estimate_token_price(btc_move_pct: float, elapsed_sec: int) -> tuple[float, float]:
    """
    Estimate UP/DOWN token prices based on BTC movement.

    When BTC moves, market makers adjust prices. Rough model:
    - 0% move -> UP=$0.50, DOWN=$0.50
    - +0.1% move -> UP~$0.60, DOWN~$0.40
    - +0.2% move -> UP~$0.70, DOWN~$0.30
    - +0.3% move -> UP~$0.80, DOWN~$0.20
    - +0.5%+ move -> UP~$0.90, DOWN~$0.10

    Time decay: later in the market, prices are more extreme.
    """
    abs_move = abs(btc_move_pct)
    time_factor = min(elapsed_sec / 300.0, 1.0)  # 0 at start, 1 at end

    # Base probability from BTC movement
    if abs_move >= 0.5:
        prob_move_side = 0.90
    elif abs_move >= 0.3:
        prob_move_side = 0.80
    elif abs_move >= 0.2:
        prob_move_side = 0.70
    elif abs_move >= 0.1:
        prob_move_side = 0.60
    elif abs_move >= 0.05:
        prob_move_side = 0.55
    else:
        prob_move_side = 0.50

    # Time makes it more extreme (closer to end = more certain)
    prob_move_side = 0.50 + (prob_move_side - 0.50) * (0.6 + 0.4 * time_factor)

    if btc_move_pct >= 0:
        up_price = round(prob_move_side, 2)
        down_price = round(1.0 - prob_move_side, 2)
    else:
        down_price = round(prob_move_side, 2)
        up_price = round(1.0 - prob_move_side, 2)

    # Clamp
    up_price = max(0.02, min(0.98, up_price))
    down_price = max(0.02, min(0.98, down_price))

    return up_price, down_price


def simulate_5min_market(klines_1m: list[dict], start_idx: int) -> dict | None:
    """
    Simulate a single 5-minute market using 1-minute klines.

    Returns dict with market data at various time points.
    """
    if start_idx + 5 > len(klines_1m):
        return None

    candles = klines_1m[start_idx : start_idx + 5]
    market_open = candles[0]["open"]
    market_close = candles[-1]["close"]

    # BTC went up or down in this 5min?
    actual_up = market_close >= market_open
    actual_direction = "up" if actual_up else "down"
    total_move_pct = ((market_close - market_open) / market_open) * 100

    # Snapshot at t=120s (after 2 minutes = candle index 2)
    if len(candles) < 3:
        return None

    btc_at_2min = candles[1]["close"]  # end of minute 2
    move_at_2min = ((btc_at_2min - market_open) / market_open) * 100

    # Snapshot at t=180s (after 3 minutes)
    btc_at_3min = candles[2]["close"]
    move_at_3min = ((btc_at_3min - market_open) / market_open) * 100

    # Estimate token prices at t=120s
    up_price_2m, down_price_2m = estimate_token_price(move_at_2min, 120)

    # Did reversal happen? (BTC moved one way in first 2min but ended opposite)
    mid_direction = "up" if move_at_2min > 0 else "down"
    reversed = mid_direction != actual_direction

    hour = datetime.fromtimestamp(
        candles[0]["open_time"] / 1000, tz=timezone.utc
    ).hour

    return {
        "open_time": candles[0]["open_time"],
        "hour": hour,
        "market_open": market_open,
        "market_close": market_close,
        "actual_direction": actual_direction,
        "total_move_pct": total_move_pct,
        "move_at_2min": move_at_2min,
        "move_at_3min": move_at_3min,
        "mid_direction": mid_direction,
        "reversed": reversed,
        "up_price_2m": up_price_2m,
        "down_price_2m": down_price_2m,
    }


def run_backtest(hours: int = 168, min_move_pct: float = 0.08):
    """Run reversal sniper backtest."""
    klines = fetch_1m_klines(hours)

    if len(klines) < 10:
        print("Not enough data")
        return

    # Build 5-minute markets (every 5 minutes, aligned to 5min boundaries)
    markets = []
    i = 0
    while i + 5 <= len(klines):
        # Align to 5-minute boundary
        ts = klines[i]["open_time"] // 1000
        if ts % 300 == 0:
            mkt = simulate_5min_market(klines, i)
            if mkt:
                markets.append(mkt)
            i += 5
        else:
            i += 1

    print(f"\nSimulated {len(markets)} 5-minute markets")
    print()

    # ============================================================
    # Strategy A: Current bot (buy momentum side at expensive price)
    # ============================================================
    a_trades = 0
    a_wins = 0
    a_pnl = 0.0
    a_skips = 0

    for mkt in markets:
        if abs(mkt["move_at_2min"]) < min_move_pct:
            a_skips += 1
            continue

        # Bot buys the momentum side (expensive)
        if mkt["mid_direction"] == "up":
            entry_price = mkt["up_price_2m"]  # expensive
        else:
            entry_price = mkt["down_price_2m"]  # expensive

        # Skip if too expensive (MAX_EXEC_PRICE)
        if entry_price > 0.65:
            a_skips += 1
            continue

        won = mkt["mid_direction"] == mkt["actual_direction"]
        shares = 5
        pnl = ((1.0 - entry_price) * shares) if won else (-entry_price * shares)

        a_trades += 1
        a_pnl += pnl
        if won:
            a_wins += 1

    # ============================================================
    # Strategy B: Reversal sniper (buy cheap opposite side)
    # ============================================================
    b_trades = 0
    b_wins = 0
    b_pnl = 0.0
    b_skips = 0
    b_by_entry = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    for mkt in markets:
        if abs(mkt["move_at_2min"]) < min_move_pct:
            b_skips += 1
            continue

        # Buy the OPPOSITE (cheap) side
        if mkt["mid_direction"] == "up":
            entry_price = mkt["down_price_2m"]  # cheap
            won = mkt["actual_direction"] == "down"  # reversal
        else:
            entry_price = mkt["up_price_2m"]  # cheap
            won = mkt["actual_direction"] == "up"  # reversal

        # Only buy if cheap enough (< $0.35)
        if entry_price > 0.35:
            b_skips += 1
            continue

        # Calculate shares for $1.00 investment
        budget = 1.00
        shares = int(budget / entry_price)
        if shares < 1:
            b_skips += 1
            continue

        pnl = ((1.0 - entry_price) * shares) if won else (-entry_price * shares)

        b_trades += 1
        b_pnl += pnl
        if won:
            b_wins += 1

        # Track by entry price bucket
        bucket = f"${entry_price:.2f}"
        b_by_entry[bucket]["trades"] += 1
        b_by_entry[bucket]["pnl"] += pnl
        if won:
            b_by_entry[bucket]["wins"] += 1

    # ============================================================
    # Strategy C: Reversal with larger move filter (>0.15%)
    # ============================================================
    c_trades = 0
    c_wins = 0
    c_pnl = 0.0
    c_skips = 0

    for mkt in markets:
        if abs(mkt["move_at_2min"]) < 0.15:  # stronger filter
            c_skips += 1
            continue

        # Buy the OPPOSITE (cheap) side
        if mkt["mid_direction"] == "up":
            entry_price = mkt["down_price_2m"]
            won = mkt["actual_direction"] == "down"
        else:
            entry_price = mkt["up_price_2m"]
            won = mkt["actual_direction"] == "up"

        if entry_price > 0.30:
            c_skips += 1
            continue

        budget = 1.00
        shares = int(budget / entry_price)
        if shares < 1:
            c_skips += 1
            continue

        pnl = ((1.0 - entry_price) * shares) if won else (-entry_price * shares)

        c_trades += 1
        c_pnl += pnl
        if won:
            c_wins += 1

    # ============================================================
    # Strategy D: Reversal with even larger move (>0.20%) + very cheap (<$0.20)
    # ============================================================
    d_trades = 0
    d_wins = 0
    d_pnl = 0.0

    for mkt in markets:
        if abs(mkt["move_at_2min"]) < 0.20:
            continue

        if mkt["mid_direction"] == "up":
            entry_price = mkt["down_price_2m"]
            won = mkt["actual_direction"] == "down"
        else:
            entry_price = mkt["up_price_2m"]
            won = mkt["actual_direction"] == "up"

        if entry_price > 0.20:
            continue

        budget = 1.00
        shares = int(budget / entry_price)
        if shares < 1:
            continue

        pnl = ((1.0 - entry_price) * shares) if won else (-entry_price * shares)

        d_trades += 1
        d_pnl += pnl
        if won:
            d_wins += 1

    # ============================================================
    # General reversal stats
    # ============================================================
    reversals_008 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.08 and m["reversed"])
    total_008 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.08)

    reversals_015 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.15 and m["reversed"])
    total_015 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.15)

    reversals_020 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.20 and m["reversed"])
    total_020 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.20)

    reversals_030 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.30 and m["reversed"])
    total_030 = sum(1 for m in markets if abs(m["move_at_2min"]) >= 0.30)

    # ============================================================
    # Print results
    # ============================================================
    print("=" * 70)
    print(f"  REVERSAL SNIPER BACKTEST - Last {hours}h ({len(markets)} markets)")
    print("=" * 70)

    print()
    print("  -- Reversal frequency (how often BTC reverses after 2min) --")
    print(f"  Move > 0.08%: {reversals_008}/{total_008} reversed = {reversals_008/total_008*100:.0f}%" if total_008 else "  Move > 0.08%: N/A")
    print(f"  Move > 0.15%: {reversals_015}/{total_015} reversed = {reversals_015/total_015*100:.0f}%" if total_015 else "  Move > 0.15%: N/A")
    print(f"  Move > 0.20%: {reversals_020}/{total_020} reversed = {reversals_020/total_020*100:.0f}%" if total_020 else "  Move > 0.20%: N/A")
    print(f"  Move > 0.30%: {reversals_030}/{total_030} reversed = {reversals_030/total_030*100:.0f}%" if total_030 else "  Move > 0.30%: N/A")

    print()
    print("  " + "=" * 66)
    print("  STRATEGY A: Current bot (buy momentum side, max $0.65)")
    print("  " + "-" * 66)
    if a_trades > 0:
        a_wr = a_wins / a_trades * 100
        a_avg_pnl = a_pnl / a_trades
        print(f"  Trades: {a_trades}  W/L: {a_wins}/{a_trades-a_wins}  WR: {a_wr:.1f}%")
        print(f"  PnL: ${a_pnl:+.2f}  Avg: ${a_avg_pnl:+.2f}/trade  Skipped: {a_skips}")
    else:
        print(f"  No trades (all {a_skips} skipped)")

    print()
    print("  " + "=" * 66)
    print("  STRATEGY B: Reversal sniper (buy cheap side <$0.35, move >0.08%)")
    print("  " + "-" * 66)
    if b_trades > 0:
        b_wr = b_wins / b_trades * 100
        b_avg_pnl = b_pnl / b_trades
        print(f"  Trades: {b_trades}  W/L: {b_wins}/{b_trades-b_wins}  WR: {b_wr:.1f}%")
        print(f"  PnL: ${b_pnl:+.2f}  Avg: ${b_avg_pnl:+.2f}/trade ($1/trade budget)  Skipped: {b_skips}")
        print()
        print("  Entry price breakdown:")
        for bucket in sorted(b_by_entry):
            d = b_by_entry[bucket]
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            print(f"    {bucket}: {d['trades']} trades, {d['wins']}W ({wr:.0f}% WR), PnL ${d['pnl']:+.2f}")
    else:
        print(f"  No trades (all {b_skips} skipped)")

    print()
    print("  " + "=" * 66)
    print("  STRATEGY C: Reversal sniper (cheap <$0.30, move >0.15%)")
    print("  " + "-" * 66)
    if c_trades > 0:
        c_wr = c_wins / c_trades * 100
        c_avg_pnl = c_pnl / c_trades
        print(f"  Trades: {c_trades}  W/L: {c_wins}/{c_trades-c_wins}  WR: {c_wr:.1f}%")
        print(f"  PnL: ${c_pnl:+.2f}  Avg: ${c_avg_pnl:+.2f}/trade ($1/trade budget)  Skipped: {c_skips}")
    else:
        print(f"  No trades")

    print()
    print("  " + "=" * 66)
    print("  STRATEGY D: Deep reversal (very cheap <$0.20, move >0.20%)")
    print("  " + "-" * 66)
    if d_trades > 0:
        d_wr = d_wins / d_trades * 100
        d_avg_pnl = d_pnl / d_trades
        print(f"  Trades: {d_trades}  W/L: {d_wins}/{d_trades-d_wins}  WR: {d_wr:.1f}%")
        print(f"  PnL: ${d_pnl:+.2f}  Avg: ${d_avg_pnl:+.2f}/trade ($1/trade budget)")
    else:
        print(f"  No trades")

    print()
    print("=" * 70)

    # Compare
    print()
    print("  COMPARISON (per trade EV):")
    if a_trades > 0:
        print(f"    A) Momentum buy:       ${a_pnl/a_trades:+.2f}/trade  ({a_wins}/{a_trades} = {a_wins/a_trades*100:.0f}% WR)")
    if b_trades > 0:
        print(f"    B) Reversal >0.08%:    ${b_pnl/b_trades:+.2f}/trade  ({b_wins}/{b_trades} = {b_wins/b_trades*100:.0f}% WR)")
    if c_trades > 0:
        print(f"    C) Reversal >0.15%:    ${c_pnl/c_trades:+.2f}/trade  ({c_wins}/{c_trades} = {c_wins/c_trades*100:.0f}% WR)")
    if d_trades > 0:
        print(f"    D) Deep reversal:      ${d_pnl/d_trades:+.2f}/trade  ({d_wins}/{d_trades} = {d_wins/d_trades*100:.0f}% WR)")
    print()
    print("=" * 70)


def main():
    hours = 168  # 7 days default

    if "--hours" in sys.argv:
        idx = sys.argv.index("--hours")
        if idx + 1 < len(sys.argv):
            hours = int(sys.argv[idx + 1])

    run_backtest(hours=hours)


if __name__ == "__main__":
    main()
