#!/usr/bin/env python3
"""
PolyBMiCoB Backtester

Simulates the bot's signal engine against historical BTC 5-minute data.
Uses Binance klines to determine what BTC did every 5 minutes, then
checks if the signal engine would have predicted correctly.

Usage:
  python scripts/backtest.py                   # last 24 hours
  python scripts/backtest.py --hours 72        # last 3 days
  python scripts/backtest.py --hours 168       # last week

Output:
  - Win/Loss rate for simulated signals
  - Breakdown by direction (UP vs DOWN)
  - P&L simulation at $0.50 entry price
  - Best/worst hours of day
"""

import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from lib.signal_engine import generate_signal


def fetch_binance_klines(hours: int = 24) -> list[dict]:
    """
    Fetch 5-minute klines from Binance for the last N hours.

    Returns list of dicts with:
      - open_time: unix timestamp (ms)
      - open, high, low, close: float prices
      - volume: float
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (hours * 3600 * 1000)

    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        resp = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "5m",
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

    return all_klines


def compute_momentum(klines: list[dict], idx: int) -> float:
    """Compute 5-minute momentum (% change) at position idx using previous candle."""
    if idx < 1:
        return 0.0
    prev = klines[idx - 1]
    return ((prev["close"] - prev["open"]) / prev["open"]) * 100


def classify_trend(momentum: float) -> str:
    """Classify trend from momentum percentage."""
    if momentum > 0.05:
        return "up"
    elif momentum < -0.05:
        return "down"
    return "flat"


def run_backtest(hours: int = 24, min_edge: float = 0.05) -> dict:
    """
    Run backtest simulation.

    For each 5-minute candle:
      1. Compute momentum from previous candle
      2. Ask signal engine what it would do (with synthetic 50/50 prices)
      3. Check actual outcome (did BTC go up or down in this 5 min?)
      4. Record win/loss

    Returns dict with results.
    """
    print(f"Fetching {hours}h of BTC 5m klines from Binance...")
    klines = fetch_binance_klines(hours)
    print(f"Got {len(klines)} candles")

    if len(klines) < 2:
        print("Not enough data")
        return {}

    wins = 0
    losses = 0
    skips = 0
    total_pnl = 0.0
    up_wins = 0
    up_losses = 0
    down_wins = 0
    down_losses = 0
    hourly_results = defaultdict(lambda: {"wins": 0, "losses": 0})

    for i in range(1, len(klines)):
        candle = klines[i]
        momentum = compute_momentum(klines, i)
        trend = classify_trend(momentum)

        # Actual outcome: did BTC close higher than open?
        actual_up = candle["close"] >= candle["open"]
        actual_direction = "up" if actual_up else "down"

        # Simulate signal engine with synthetic 50/50 market
        # (as if Up and Down tokens are both priced at $0.50)
        signal = generate_signal(
            momentum_pct=momentum,
            trend=trend,
            up_price=0.50,
            down_price=0.50,
            up_token_id="sim_up",
            down_token_id="sim_down",
            market_slug="backtest",
            orderbook=None,
            fear_greed_value=None,
            min_edge=min_edge,
        )

        if signal is None:
            skips += 1
            continue

        predicted = signal.direction
        won = predicted == actual_direction
        entry = signal.entry_price
        pnl = (1.0 - entry) if won else (-entry)

        total_pnl += pnl

        if won:
            wins += 1
            if predicted == "up":
                up_wins += 1
            else:
                down_wins += 1
        else:
            losses += 1
            if predicted == "up":
                up_losses += 1
            else:
                down_losses += 1

        # Track by hour of day (UTC)
        hour = datetime.fromtimestamp(candle["open_time"] / 1000, tz=timezone.utc).hour
        if won:
            hourly_results[hour]["wins"] += 1
        else:
            hourly_results[hour]["losses"] += 1

    total_signals = wins + losses
    win_rate = wins / total_signals if total_signals > 0 else 0

    return {
        "hours": hours,
        "candles": len(klines),
        "signals": total_signals,
        "skips": skips,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "up_wins": up_wins,
        "up_losses": up_losses,
        "down_wins": down_wins,
        "down_losses": down_losses,
        "hourly": dict(hourly_results),
        "min_edge": min_edge,
    }


def print_results(r: dict) -> None:
    """Pretty-print backtest results."""
    if not r:
        return

    print()
    print("=" * 60)
    print(f"  BACKTEST RESULTS - Last {r['hours']}h ({r['candles']} candles)")
    print("=" * 60)
    print()
    print(f"  Signals generated:  {r['signals']} (skipped {r['skips']}, min_edge={r['min_edge']:.0%})")
    print(f"  Wins:               {r['wins']}")
    print(f"  Losses:             {r['losses']}")
    print(f"  Win Rate:           {r['win_rate']:.1%}")
    print(f"  Simulated P&L:      ${r['total_pnl']:+.2f} (at ~$0.50/trade)")
    print()

    up_total = r["up_wins"] + r["up_losses"]
    down_total = r["down_wins"] + r["down_losses"]
    up_wr = r["up_wins"] / up_total if up_total > 0 else 0
    down_wr = r["down_wins"] / down_total if down_total > 0 else 0

    print(f"  UP bets:    {r['up_wins']}W / {r['up_losses']}L  ({up_wr:.0%}) [{up_total} total]")
    print(f"  DOWN bets:  {r['down_wins']}W / {r['down_losses']}L  ({down_wr:.0%}) [{down_total} total]")
    print()

    # Best/worst hours
    hourly = r.get("hourly", {})
    if hourly:
        print("  Hour (UTC)  Signals  WinRate")
        print("  " + "-" * 35)
        for hour in sorted(hourly.keys()):
            h = hourly[hour]
            total = h["wins"] + h["losses"]
            wr = h["wins"] / total if total > 0 else 0
            bar = "#" * int(wr * 20)
            print(f"  {hour:02d}:00       {total:4d}     {wr:5.0%}  {bar}")

    print()
    print("=" * 60)

    # Verdict
    if r["win_rate"] > 0.55:
        print("  VERDICT: Strategy looks promising (>55% win rate)")
    elif r["win_rate"] > 0.50:
        print("  VERDICT: Slightly profitable but thin edge")
    elif r["win_rate"] > 0.45:
        print("  VERDICT: Near break-even - needs improvement")
    else:
        print("  VERDICT: Strategy is losing money - reconsider")
    print("=" * 60)


def main():
    hours = 24
    min_edge = 0.05

    # Parse args
    if "--hours" in sys.argv:
        idx = sys.argv.index("--hours")
        if idx + 1 < len(sys.argv):
            hours = int(sys.argv[idx + 1])

    if "--edge" in sys.argv:
        idx = sys.argv.index("--edge")
        if idx + 1 < len(sys.argv):
            min_edge = float(sys.argv[idx + 1])

    results = run_backtest(hours=hours, min_edge=min_edge)
    print_results(results)


if __name__ == "__main__":
    main()
