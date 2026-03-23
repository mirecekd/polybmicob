#!/usr/bin/env python3
"""
PolyBMiCoB Trade Dashboard

Simple HTTP server with big performance cards and collapsible details.

Usage:
  python web/dashboard.py [--port 8005]
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.resolution_tracker import compute_resolution_stats
from lib.stats_collector import load_today_stats, load_wallet_balance

# MM Pair mode detection
MM_PAIR_ENABLED = os.environ.get("MM_PAIR_ENABLED", "false").lower() == "true"

DATA_DIR = Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "btc_trades.json"
LOG_FILE = DATA_DIR / "btc_bot.log"
PORT = int(os.environ.get("DASHBOARD_PORT", "8005"))

# Hour-of-day trading filter (same logic as btc_bot.py)
_TRADING_HOURS_STR = os.environ.get("TRADING_HOURS_UTC", "")
TRADING_HOURS: set[int] | None = (
    {int(h.strip()) for h in _TRADING_HOURS_STR.split(",") if h.strip()}
    if _TRADING_HOURS_STR.strip()
    else None
)


def load_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    try:
        return json.loads(TRADES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def get_log_tail(lines: int = 30) -> str:
    if not LOG_FILE.exists():
        return "No log file yet."
    try:
        text = LOG_FILE.read_text()
        all_lines = text.strip().split("\n")
        return "\n".join(all_lines[-lines:])
    except OSError:
        return "Error reading log."


def parse_today_activity() -> dict:
    """Parse today's bot log for activity stats (cycles, skips, signals, errors)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = {
        "today": today,
        "total_cycles": 0,
        "momentum_skips": 0,
        "no_signal": 0,
        "pre_signals": 0,
        "inplay_signals": 0,
        "orders_filled": 0,
        "orders_not_filled": 0,
        "orders_no_liquidity": 0,
        "orders_failed": 0,
        "trades_resolved": 0,
        "hourly": {},  # hour -> {cycles, skips, signals, filled, not_filled}
        "last_order_time": None,
        "last_signal_time": None,
        "fail_reasons": [],
        "inplay_events": [],  # list of {time, market, dir, edge, btc_move, outcome, detail}
    }

    # State for correlating in-play signals with outcomes
    pending_ip_signal = None

    if not LOG_FILE.exists():
        return result

    try:
        text = LOG_FILE.read_text()
    except OSError:
        return result

    def _resolve_pending(outcome: str, detail: str = ""):
        """Resolve a pending in-play signal with its outcome."""
        nonlocal pending_ip_signal
        if pending_ip_signal:
            pending_ip_signal["outcome"] = outcome
            pending_ip_signal["detail"] = detail
            result["inplay_events"].append(pending_ip_signal)
            pending_ip_signal = None

    for line in text.split("\n"):
        if not line.startswith(today):
            continue

        # Extract hour
        try:
            hour = line[11:13]
        except IndexError:
            continue

        if hour not in result["hourly"]:
            result["hourly"][hour] = {"cycles": 0, "skips": 0, "signals": 0, "filled": 0, "rejected": 0}

        if "Found" in line and "upcoming BTC 5m markets" in line:
            result["total_cycles"] += 1
            result["hourly"][hour]["cycles"] += 1

        elif "threshold, skipping pre-market" in line:
            result["momentum_skips"] += 1
            result["hourly"][hour]["skips"] += 1

        elif "no signal" in line:
            result["no_signal"] += 1

        elif "SIGNAL:" in line and "IN-PLAY" not in line:
            result["pre_signals"] += 1
            result["hourly"][hour]["signals"] += 1
            result["last_signal_time"] = line[:19]

        elif "IN-PLAY SIGNAL:" in line:
            # If there's still a pending signal without outcome, mark it unknown
            _resolve_pending("unknown")
            result["inplay_signals"] += 1
            result["hourly"][hour]["signals"] += 1
            result["last_signal_time"] = line[:19]
            # Parse: IN-PLAY SIGNAL: DOWN on btc-updown-5m-XXX  edge=7.0%  BTC -0.137% (90s elapsed)
            ip_info = {"time": line[:19], "dir": "?", "market": "", "edge": "", "btc_move": "", "elapsed": ""}
            try:
                after_sig = line.split("IN-PLAY SIGNAL:")[1].strip()
                parts = after_sig.split()
                ip_info["dir"] = parts[0]  # UP or DOWN
                ip_info["market"] = parts[2].replace("btc-updown-5m-", "5m-") if len(parts) > 2 else ""
                for p in parts:
                    if p.startswith("edge="):
                        ip_info["edge"] = p.replace("edge=", "")
                    elif p.startswith("+") or (p.startswith("-") and "%" in p):
                        ip_info["btc_move"] = p
                # Extract elapsed
                if "(" in after_sig and "elapsed" in after_sig:
                    ip_info["elapsed"] = after_sig.split("(")[1].split(")")[0]
            except (IndexError, ValueError):
                pass
            pending_ip_signal = ip_info

        elif "ORDER FILLED" in line or "ORDER PLACED:" in line:
            result["orders_filled"] += 1
            result["hourly"][hour]["filled"] += 1
            result["last_order_time"] = line[:19]
            _resolve_pending("filled")

        elif "ORDER NOT FILLED" in line:
            result["orders_not_filled"] += 1
            result["hourly"][hour]["rejected"] += 1
            result["fail_reasons"].append("not_filled")
            _resolve_pending("not_filled", "FOK not filled")

        elif "No asks on orderbook" in line:
            result["orders_no_liquidity"] += 1
            result["hourly"][hour]["rejected"] += 1
            result["fail_reasons"].append("no_asks")
            _resolve_pending("no_asks", "Empty orderbook")

        elif "Insufficient liquidity" in line:
            result["orders_no_liquidity"] += 1
            result["hourly"][hour]["rejected"] += 1
            result["fail_reasons"].append("low_liq")
            # Extract detail like "need 5 shares, only 0 available @ $0.95"
            detail = ""
            if ":" in line.split("Insufficient liquidity")[1]:
                detail = line.split("Insufficient liquidity:")[1].strip()
            _resolve_pending("low_liq", detail or "Insufficient liquidity")

        elif "Trade failed:" in line:
            result["orders_failed"] += 1
            result["hourly"][hour]["rejected"] += 1
            detail = line.split("Trade failed:")[1].strip() if "Trade failed:" in line else ""
            if "minimum:" in line:
                result["fail_reasons"].append("min_size")
                _resolve_pending("min_size", detail)
            elif "balance" in line.lower():
                result["fail_reasons"].append("balance")
                _resolve_pending("balance", detail)
            elif "FOK" in line or "fully filled" in line:
                result["fail_reasons"].append("fok_killed")
                _resolve_pending("fok_killed", "FOK order killed")
            elif "Request exception" in line:
                result["fail_reasons"].append("api_error")
                _resolve_pending("api_error", "API request exception")
            else:
                result["fail_reasons"].append("error")
                _resolve_pending("error", detail)

        elif "Resolved" in line and "trade(s)" in line:
            result["trades_resolved"] += 1

    # Resolve any trailing pending signal
    _resolve_pending("unknown")

    return result


def compute_stats(trades: list[dict]) -> dict:
    total = len(trades)
    live_trades = [t for t in trades if not t.get("dry_run", True)]
    dry_trades = [t for t in trades if t.get("dry_run", True)]

    total_invested = sum(t.get("entry_price", 0) for t in live_trades)
    avg_edge = sum(t.get("edge", 0) for t in trades) / total if total > 0 else 0
    avg_confidence = sum(t.get("confidence", 0) for t in trades) / total if total > 0 else 0

    res = compute_resolution_stats(TRADES_FILE)

    return {
        "total_trades": total,
        "live_trades": len(live_trades),
        "dry_trades": len(dry_trades),
        "total_invested": total_invested,
        "avg_edge": avg_edge,
        "avg_confidence": avg_confidence,
        "wins": res.wins,
        "losses": res.losses,
        "win_rate": res.win_rate,
        "total_pnl": res.total_pnl,
        "resolved": res.resolved,
        "unresolved": res.unresolved,
        "up_wins": res.up_wins,
        "up_losses": res.up_losses,
        "down_wins": res.down_wins,
        "down_losses": res.down_losses,
        "streak": res.streak,
    }


def _extract_pair_cost(trade: dict) -> float | None:
    """Extract pair_cost from trade reason string or hedge_pair_cost field."""
    # First check hedge_pair_cost (set when pair is completed)
    hpc = trade.get("hedge_pair_cost")
    if hpc is not None:
        return float(hpc)
    # Parse from reason: "MM-pair off-hours (pair_cost=$0.98)"
    reason = trade.get("reason", "")
    m = re.search(r"pair_cost=\$([0-9.]+)", reason)
    if m:
        return float(m.group(1))
    return None


def _infer_shares(trade: dict) -> float:
    """Infer number of shares from a trade record."""
    price = trade.get("entry_price", 0)
    pnl = trade.get("pnl")
    won = trade.get("won")
    if pnl is not None and price > 0:
        if won is False:
            return round(abs(pnl) / price, 1)
        elif won is True and price < 1.0:
            return round(pnl / (1.0 - price), 1)
    # Fallback: assume 5 shares (common minimum)
    return 5.0


def compute_mm_stats(trades: list[dict]) -> dict:
    """Compute MM pair-specific stats from trade records.

    For locked pairs (both sides held), real profit = shares * (1.00 - pair_cost).
    For partials (one side only), P&L is directional (trade.pnl).
    """
    mm_modes = {"mm-pair", "mm-pair-offhours", "mm-pair-arb", "mm-pair-complete"}
    mm_trades = [t for t in trades if t.get("mode", "") in mm_modes and not t.get("dry_run", True)]

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mm_today = [t for t in mm_trades if t.get("timestamp", "").startswith(today_str)]

    # Group by slug to identify pairs
    slugs_all: dict[str, list[dict]] = {}
    for t in mm_trades:
        slug = t.get("slug", "")
        if slug not in slugs_all:
            slugs_all[slug] = []
        slugs_all[slug].append(t)

    slugs_today: dict[str, list[dict]] = {}
    for t in mm_today:
        slug = t.get("slug", "")
        if slug not in slugs_today:
            slugs_today[slug] = []
        slugs_today[slug].append(t)

    def _analyze_pairs(slug_groups: dict[str, list[dict]]) -> dict:
        total_pairs = 0
        locked_pairs = 0  # both sides held (arb / hedged / completed)
        partials = 0
        completed = 0  # partial -> full via _complete_mm_pair
        pair_costs: list[float] = []
        real_pair_profit = 0.0  # sum of shares * (1 - pair_cost) for locked pairs
        partial_pnl = 0.0  # directional pnl for unlocked partials
        pair_details: list[dict] = []  # per-pair breakdown

        for slug, tlist in slug_groups.items():
            initial = [t for t in tlist if t.get("mode") in ("mm-pair", "mm-pair-offhours", "mm-pair-arb")]
            completions = [t for t in tlist if t.get("mode") == "mm-pair-complete"]

            if not initial:
                continue
            total_pairs += 1

            t0 = initial[0]
            is_hedged = t0.get("hedged", False)
            is_arb = t0.get("mode") == "mm-pair-arb"
            shares = _infer_shares(t0)

            pc = _extract_pair_cost(t0)

            pair_info = {
                "slug": slug,
                "time": t0.get("timestamp", "")[:19],
                "direction": t0.get("direction", "?"),
                "entry_price": t0.get("entry_price", 0),
                "pair_cost": pc,
                "shares": shares,
                "locked": False,
                "lock_type": "partial",
                "pair_profit_usd": 0.0,
                "resolved": any(t.get("resolved") is not None for t in tlist),
                "won": None,
            }

            is_locked = False
            lock_type = "partial"

            if is_arb:
                is_locked = True
                lock_type = "arb"
                locked_pairs += 1
                if pc is not None:
                    pair_costs.append(pc)
            elif completions:
                is_locked = True
                lock_type = "complete"
                completed += 1
                locked_pairs += 1
                cpc = _extract_pair_cost(completions[0])
                if cpc is not None:
                    pair_costs.append(cpc)
                    pc = cpc
                elif pc is not None:
                    pair_costs.append(pc)
            elif is_hedged:
                is_locked = True
                lock_type = "hedged"
                locked_pairs += 1
                hpc = t0.get("hedge_pair_cost")
                if hpc is not None:
                    pc = float(hpc)
                    pair_costs.append(pc)
                elif pc is not None:
                    pair_costs.append(pc)
            else:
                partials += 1
                if pc is not None:
                    pair_costs.append(pc)

            pair_info["locked"] = is_locked
            pair_info["lock_type"] = lock_type
            pair_info["pair_cost"] = pc

            if is_locked and pc is not None:
                profit = shares * (1.00 - pc)
                real_pair_profit += profit
                pair_info["pair_profit_usd"] = round(profit, 2)
            elif not is_locked:
                # Partial: use directional pnl from resolved trades
                for t in tlist:
                    if t.get("resolved") is not None:
                        p = t.get("pnl", 0) or 0
                        partial_pnl += p
                        pair_info["pair_profit_usd"] = round(p, 2)
                        pair_info["won"] = t.get("won")

            pair_details.append(pair_info)

        avg_pair_cost = sum(pair_costs) / len(pair_costs) if pair_costs else 0
        avg_spread = (1.00 - avg_pair_cost) if avg_pair_cost > 0 else 0
        fill_rate = locked_pairs / total_pairs if total_pairs > 0 else 0
        total_pnl = round(real_pair_profit + partial_pnl, 2)

        return {
            "total_pairs": total_pairs,
            "locked_pairs": locked_pairs,
            "both_filled": locked_pairs,  # backward compat alias
            "partials": partials,
            "completed": completed,
            "avg_pair_cost": avg_pair_cost,
            "avg_spread": avg_spread,
            "fill_rate": fill_rate,
            "total_pnl": total_pnl,
            "real_pair_profit": round(real_pair_profit, 2),
            "partial_pnl": round(partial_pnl, 2),
            "pair_costs": pair_costs,
            "pair_details": pair_details,
        }

    all_stats = _analyze_pairs(slugs_all)
    today_stats = _analyze_pairs(slugs_today)

    # Pending (unresolved) MM trades
    pending = sum(1 for t in mm_trades if t.get("resolved") is None)

    return {
        "all": all_stats,
        "today": today_stats,
        "total_mm_trades": len(mm_trades),
        "today_mm_trades": len(mm_today),
        "pending": pending,
        "mm_trades": mm_trades,
    }


def _parse_trade_timestamps(trades: list[dict]) -> list[datetime]:
    """Parse timestamps from trades into datetime objects."""
    timestamps = []
    for t in trades:
        ts_str = t.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_str)
            timestamps.append(dt)
        except (ValueError, TypeError):
            timestamps.append(None)
    return timestamps


def _parse_market_time(slug: str) -> str | None:
    """Extract market time from slug like 'btc-updown-5m-1773641100'."""
    if not slug:
        return None
    parts = slug.split("-")
    if len(parts) >= 4:
        try:
            epoch = int(parts[-1])
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            return dt.strftime("%H:%M")
        except (ValueError, OSError):
            pass
    return None


def _generate_time_ticks(timestamps: list[datetime | None], pad_l: int, chart_w: int, y_pos: float) -> str:
    """Generate SVG time tick marks and labels for the X-axis."""
    valid = [(i, ts) for i, ts in enumerate(timestamps) if ts is not None]
    if len(valid) < 2:
        return ""

    n = len(timestamps)
    first_ts = valid[0][1]
    last_ts = valid[-1][1]
    total_seconds = (last_ts - first_ts).total_seconds()

    if total_seconds <= 0:
        return ""

    # Decide tick interval based on total time span
    if total_seconds <= 3600 * 2:       # <= 2h: every 15 min
        interval_s = 900
        fmt = "%H:%M"
    elif total_seconds <= 3600 * 6:     # <= 6h: every 1h
        interval_s = 3600
        fmt = "%H:%M"
    elif total_seconds <= 3600 * 24:    # <= 24h: every 2h
        interval_s = 7200
        fmt = "%H:%M"
    elif total_seconds <= 3600 * 48:    # <= 48h: every 4h
        interval_s = 14400
        fmt = "%d %b %H:%M"
    else:                                # > 48h: every 8h
        interval_s = 28800
        fmt = "%d %b %H:%M"

    # Find first tick: round up first_ts to next interval boundary
    epoch_first = first_ts.timestamp()
    first_tick_epoch = ((int(epoch_first) // interval_s) + 1) * interval_s

    svg_parts = []
    tick_epoch = first_tick_epoch
    last_tick_epoch = last_ts.timestamp()

    while tick_epoch <= last_tick_epoch:
        # Map this tick time to x position via interpolation
        frac = (tick_epoch - epoch_first) / total_seconds
        x = pad_l + frac * chart_w

        tick_dt = datetime.fromtimestamp(tick_epoch, tz=timezone.utc)
        label = tick_dt.strftime(fmt)

        # Vertical grid line
        svg_parts.append(
            f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{y_pos:.1f}" '
            f'stroke="#30363d" stroke-width="0.5" opacity="0.6"/>'
        )
        # Label
        svg_parts.append(
            f'<text x="{x:.1f}" y="{y_pos + 11:.1f}" fill="#6e7681" font-size="9" '
            f'text-anchor="middle">{label}</text>'
        )

        tick_epoch += interval_s

    return "\n      ".join(svg_parts)


def render_charts(trades: list[dict]) -> str:
    """Render inline SVG charts for resolved trades (last 48h or last 50)."""
    resolved = [t for t in trades if t.get("resolved") is not None and not t.get("dry_run", True)]
    if len(resolved) < 2:
        return '<div class="muted" style="text-align:center;padding:20px;">Need at least 2 resolved trades for charts</div>'

    # Use last 50 resolved trades
    recent = resolved[-50:]

    # Parse timestamps for time axis
    timestamps = _parse_trade_timestamps(recent)

    # ── Chart 1: Cumulative P&L ──────────────────────────────
    w, h = 1100, 185
    pad_l, pad_r, pad_t, pad_b = 50, 20, 15, 48

    cumulative = []
    running = 0.0
    for t in recent:
        running += t.get("pnl", 0)
        cumulative.append(round(running, 2))

    min_pnl = min(cumulative)
    max_pnl = max(cumulative)
    pnl_range = max(max_pnl - min_pnl, 0.01)

    chart_w = w - pad_l - pad_r
    chart_h = h - pad_t - pad_b

    def pnl_x(i):
        return pad_l + (i / max(len(cumulative) - 1, 1)) * chart_w

    def pnl_y(val):
        return pad_t + chart_h - ((val - min_pnl) / pnl_range) * chart_h

    # Zero line
    zero_y = pnl_y(0) if min_pnl <= 0 <= max_pnl else None

    # Build polyline points
    points = " ".join(f"{pnl_x(i):.1f},{pnl_y(v):.1f}" for i, v in enumerate(cumulative))

    # Fill area (to zero or bottom)
    fill_base_y = zero_y if zero_y is not None else (pad_t + chart_h)
    fill_points = f"{pnl_x(0):.1f},{fill_base_y:.1f} {points} {pnl_x(len(cumulative)-1):.1f},{fill_base_y:.1f}"

    final_color = "#3fb950" if cumulative[-1] >= 0 else "#f85149"

    pnl_time_ticks = _generate_time_ticks(timestamps, pad_l, chart_w, pad_t + chart_h)

    pnl_svg = f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;">
      <rect x="{pad_l}" y="{pad_t}" width="{chart_w}" height="{chart_h}" fill="#0d1117" rx="4"/>
      {pnl_time_ticks}
      {'<line x1="' + str(pad_l) + '" y1="' + f"{zero_y:.1f}" + '" x2="' + str(w-pad_r) + '" y2="' + f"{zero_y:.1f}" + '" stroke="#30363d" stroke-dasharray="4,4"/>' if zero_y else ''}
      <polygon points="{fill_points}" fill="{final_color}" opacity="0.1"/>
      <polyline points="{points}" fill="none" stroke="{final_color}" stroke-width="2"/>
      <text x="{pad_l - 5}" y="{pnl_y(max_pnl):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${max_pnl:+.2f}</text>
      <text x="{pad_l - 5}" y="{pnl_y(min_pnl):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${min_pnl:+.2f}</text>
      <text x="{w/2}" y="{h - 3}" fill="#6e7681" font-size="10" text-anchor="middle">Cumulative P&L (last {len(recent)} trades)</text>
    </svg>"""

    # ── Chart 2: Trade dots (time vs entry price, colored by win/loss) ──
    h2 = 145
    prices = [t.get("entry_price", 0.5) for t in recent]
    min_price = min(prices) - 0.02
    max_price = max(prices) + 0.02
    price_range = max(max_price - min_price, 0.01)
    chart_h2 = h2 - pad_t - pad_b

    def dot_x(i):
        return pad_l + (i / max(len(recent) - 1, 1)) * chart_w

    def dot_y(price):
        return pad_t + chart_h2 - ((price - min_price) / price_range) * chart_h2

    # Market time labels (from slug epoch) shown above dots
    market_time_labels = ""
    for i, t in enumerate(recent):
        mt = _parse_market_time(t.get("slug", ""))
        if mt:
            x = dot_x(i)
            market_time_labels += (
                f'<text x="{x:.1f}" y="{pad_t - 2}" fill="#484f58" font-size="7" '
                f'text-anchor="middle" transform="rotate(-45,{x:.1f},{pad_t - 2})">{mt}</text>'
            )

    dots = ""
    for i, t in enumerate(recent):
        x = dot_x(i)
        y = dot_y(t.get("entry_price", 0.5))
        won = t.get("won", False)
        color = "#3fb950" if won else "#f85149"
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" opacity="0.8"/>'

    dots_time_ticks = _generate_time_ticks(timestamps, pad_l, chart_w, pad_t + chart_h2)

    dots_svg = f"""<svg viewBox="0 0 {w} {h2}" style="width:100%;height:auto;">
      <rect x="{pad_l}" y="{pad_t}" width="{chart_w}" height="{chart_h2}" fill="#0d1117" rx="4"/>
      {dots_time_ticks}
      <text x="{pad_l - 5}" y="{dot_y(max_price):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${max_price:.2f}</text>
      <text x="{pad_l - 5}" y="{dot_y(min_price):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${min_price:.2f}</text>
      {dots}
      {market_time_labels}
      <text x="{w/2}" y="{h2 - 3}" fill="#6e7681" font-size="10" text-anchor="middle">Entry price: <tspan fill="#3fb950">WIN</tspan> / <tspan fill="#f85149">LOSS</tspan> (last {len(recent)} trades) | <tspan fill="#484f58">rotated = market time UTC</tspan></text>
    </svg>"""

    # ── Chart 3: BTC price trend with trade markers ──────────
    h3 = 165
    btc_prices = [t.get("btc_price", 0) for t in recent]
    min_btc = min(btc_prices) - 50
    max_btc = max(btc_prices) + 50
    btc_range = max(max_btc - min_btc, 1)
    chart_h3 = h3 - pad_t - pad_b

    def btc_x(i):
        return pad_l + (i / max(len(recent) - 1, 1)) * chart_w

    def btc_y(price):
        return pad_t + chart_h3 - ((price - min_btc) / btc_range) * chart_h3

    # BTC price line
    btc_line = " ".join(f"{btc_x(i):.1f},{btc_y(p):.1f}" for i, p in enumerate(btc_prices))

    # Trade markers on the line
    btc_markers = ""
    for i, t in enumerate(recent):
        x = btc_x(i)
        y = btc_y(t.get("btc_price", 0))
        won = t.get("won", False)
        direction = t.get("direction", "")
        color = "#3fb950" if won else "#f85149"
        # Arrow up or down based on bet direction
        if direction == "up":
            btc_markers += f'<polygon points="{x:.1f},{y-6:.1f} {x-4:.1f},{y+2:.1f} {x+4:.1f},{y+2:.1f}" fill="{color}" opacity="0.9"/>'
        else:
            btc_markers += f'<polygon points="{x:.1f},{y+6:.1f} {x-4:.1f},{y-2:.1f} {x+4:.1f},{y-2:.1f}" fill="{color}" opacity="0.9"/>'

    # BTC trend direction
    btc_trend_color = "#3fb950" if btc_prices[-1] >= btc_prices[0] else "#f85149"

    btc_time_ticks = _generate_time_ticks(timestamps, pad_l, chart_w, pad_t + chart_h3)

    btc_svg = f"""<svg viewBox="0 0 {w} {h3}" style="width:100%;height:auto;">
      <rect x="{pad_l}" y="{pad_t}" width="{chart_w}" height="{chart_h3}" fill="#0d1117" rx="4"/>
      {btc_time_ticks}
      <polyline points="{btc_line}" fill="none" stroke="#8b949e" stroke-width="1.5" opacity="0.6"/>
      {btc_markers}
      <text x="{pad_l - 5}" y="{btc_y(max_btc):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${max_btc:,.0f}</text>
      <text x="{pad_l - 5}" y="{btc_y(min_btc):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${min_btc:,.0f}</text>
      <text x="{w/2}" y="{h3 - 3}" fill="#6e7681" font-size="10" text-anchor="middle">BTC price trend: <tspan fill="#3fb950">UP bet WIN</tspan> / <tspan fill="#f85149">DOWN bet LOSS</tspan> (last {len(recent)} trades)</text>
    </svg>"""

    return f"""<div style="display:flex;flex-direction:column;gap:12px;padding:16px;">
      {pnl_svg}
      {dots_svg}
      {btc_svg}
    </div>"""


def render_mm_charts(mm_stats: dict) -> str:
    """Render inline SVG charts for MM pair trades."""
    mm_trades = mm_stats.get("mm_trades", [])
    resolved = [t for t in mm_trades if t.get("resolved") is not None]
    if len(resolved) < 2:
        return '<div class="muted" style="text-align:center;padding:20px;">Need at least 2 resolved MM trades for charts</div>'

    recent = resolved[-50:]
    timestamps = _parse_trade_timestamps(recent)

    w, h = 1100, 185
    pad_l, pad_r, pad_t, pad_b = 50, 20, 15, 48
    chart_w = w - pad_l - pad_r
    chart_h = h - pad_t - pad_b

    # -- Chart 1: Cumulative MM P&L --
    cumulative = []
    running = 0.0
    for t in recent:
        running += t.get("pnl", 0)
        cumulative.append(round(running, 2))

    min_pnl = min(cumulative)
    max_pnl = max(cumulative)
    pnl_range = max(max_pnl - min_pnl, 0.01)

    def pnl_x(i):
        return pad_l + (i / max(len(cumulative) - 1, 1)) * chart_w

    def pnl_y(val):
        return pad_t + chart_h - ((val - min_pnl) / pnl_range) * chart_h

    zero_y = pnl_y(0) if min_pnl <= 0 <= max_pnl else None
    points = " ".join(f"{pnl_x(i):.1f},{pnl_y(v):.1f}" for i, v in enumerate(cumulative))
    fill_base_y = zero_y if zero_y is not None else (pad_t + chart_h)
    fill_points = f"{pnl_x(0):.1f},{fill_base_y:.1f} {points} {pnl_x(len(cumulative)-1):.1f},{fill_base_y:.1f}"
    final_color = "#3fb950" if cumulative[-1] >= 0 else "#f85149"
    time_ticks = _generate_time_ticks(timestamps, pad_l, chart_w, pad_t + chart_h)

    pnl_svg = f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;">
      <rect x="{pad_l}" y="{pad_t}" width="{chart_w}" height="{chart_h}" fill="#0d1117" rx="4"/>
      {time_ticks}
      {'<line x1="' + str(pad_l) + '" y1="' + f"{zero_y:.1f}" + '" x2="' + str(w-pad_r) + '" y2="' + f"{zero_y:.1f}" + '" stroke="#30363d" stroke-dasharray="4,4"/>' if zero_y else ''}
      <polygon points="{fill_points}" fill="{final_color}" opacity="0.1"/>
      <polyline points="{points}" fill="none" stroke="{final_color}" stroke-width="2"/>
      <text x="{pad_l - 5}" y="{pnl_y(max_pnl):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${max_pnl:+.2f}</text>
      <text x="{pad_l - 5}" y="{pnl_y(min_pnl):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${min_pnl:+.2f}</text>
      <text x="{w/2}" y="{h - 3}" fill="#6e7681" font-size="10" text-anchor="middle">Cumulative MM P&amp;L (last {len(recent)} resolved)</text>
    </svg>"""

    # -- Chart 2: Pair cost scatter (entry_price per trade, colored by mode) --
    h2 = 145
    chart_h2 = h2 - pad_t - pad_b
    prices = [t.get("entry_price", 0.5) for t in recent]
    min_price = min(prices) - 0.02
    max_price = max(prices) + 0.02
    price_range = max(max_price - min_price, 0.01)

    def dot_x(i):
        return pad_l + (i / max(len(recent) - 1, 1)) * chart_w

    def dot_y(p):
        return pad_t + chart_h2 - ((p - min_price) / price_range) * chart_h2

    dots = ""
    for i, t in enumerate(recent):
        x = dot_x(i)
        y = dot_y(t.get("entry_price", 0.5))
        mode = t.get("mode", "")
        if mode == "mm-pair-arb":
            color = "#3fb950"  # green = arb locked
        elif mode == "mm-pair-complete":
            color = "#58a6ff"  # blue = completed in-play
        else:
            color = "#d29922"  # yellow = partial
        won = t.get("won", False)
        opacity = "0.9" if won else "0.5"
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" opacity="{opacity}"/>'

    dots_ticks = _generate_time_ticks(timestamps, pad_l, chart_w, pad_t + chart_h2)

    dots_svg = f"""<svg viewBox="0 0 {w} {h2}" style="width:100%;height:auto;">
      <rect x="{pad_l}" y="{pad_t}" width="{chart_w}" height="{chart_h2}" fill="#0d1117" rx="4"/>
      {dots_ticks}
      <text x="{pad_l - 5}" y="{dot_y(max_price):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${max_price:.2f}</text>
      <text x="{pad_l - 5}" y="{dot_y(min_price):.1f}" fill="#8b949e" font-size="10" text-anchor="end" dominant-baseline="middle">${min_price:.2f}</text>
      {dots}
      <text x="{w/2}" y="{h2 - 3}" fill="#6e7681" font-size="10" text-anchor="middle">Entry price: <tspan fill="#3fb950">arb</tspan> / <tspan fill="#58a6ff">complete</tspan> / <tspan fill="#d29922">partial</tspan> (opacity=win/loss)</text>
    </svg>"""

    return f"""<div style="display:flex;flex-direction:column;gap:12px;padding:16px;">
      {pnl_svg}
      {dots_svg}
    </div>"""


def _render_pair_row(p: dict) -> str:
    """Render a single row for the Today Pair Flow table."""
    slug = p.get("slug", "")
    short_slug = slug.replace("btc-updown-5m-", "5m-") if slug else ""
    direction = p.get("direction", "?").upper()
    dir_class = "up" if direction == "UP" else "down"
    price = p.get("entry_price", 0)
    pc = p.get("pair_cost")
    pc_str = f"${pc:.2f}" if pc is not None else "-"
    spread = (1.00 - pc) if pc is not None else 0
    spread_str = f"${spread:.3f}" if pc is not None else "-"
    shares = p.get("shares", 5.0)
    locked = p.get("locked", False)
    lock_type = p.get("lock_type", "partial")
    profit = p.get("pair_profit_usd", 0)
    resolved = p.get("resolved", False)
    time_str = p.get("time", "")

    # Locked badge
    if locked:
        locked_html = '<span style="color:#3fb950;font-weight:600">YES</span>'
    else:
        locked_html = '<span style="color:#d29922;font-weight:600">NO</span>'

    # Lock type badge
    type_colors = {"arb": "#3fb950", "hedged": "#3fb950", "complete": "#58a6ff", "partial": "#d29922"}
    type_color = type_colors.get(lock_type, "#8b949e")
    type_html = f'<span style="color:{type_color};font-size:10px;font-weight:600">{lock_type.upper()}</span>'

    # Profit
    if locked:
        profit_color = "#3fb950" if profit >= 0 else "#f85149"
        profit_html = f'<span style="color:{profit_color};font-weight:600">${profit:+.2f}</span>'
    elif resolved:
        won = p.get("won")
        profit_color = "#3fb950" if won else "#f85149"
        profit_html = f'<span style="color:{profit_color};font-weight:600">${profit:+.2f}</span>'
    else:
        profit_html = '<span class="muted">pending</span>'

    # Status
    if locked and resolved:
        status_html = '<span style="color:#3fb950;font-weight:600">CLAIMED</span>'
    elif locked:
        status_html = '<span style="color:#3fb950;font-weight:600">LOCKED</span>'
    elif resolved:
        won = p.get("won")
        status_html = f'<span style="color:{"#3fb950" if won else "#f85149"};font-weight:600">{"WIN" if won else "LOSS"}</span>'
    else:
        status_html = '<span class="muted">pending</span>'

    return f"""<tr>
      <td class="muted">{time_str[11:] if len(time_str) > 11 else time_str}</td>
      <td>{short_slug}</td>
      <td class="{dir_class}">{direction}</td>
      <td>${price:.2f}</td>
      <td>{locked_html}</td>
      <td>{type_html}</td>
      <td>{pc_str}</td>
      <td>{spread_str}</td>
      <td>{shares:.0f}</td>
      <td>{profit_html}</td>
      <td>{status_html}</td>
    </tr>"""


def render_mm_html() -> str:
    """Render MM pair-specific dashboard (when MM_PAIR_ENABLED=true)."""
    trades = load_trades()
    mm = compute_mm_stats(trades)
    log_tail = get_log_tail(40)
    wallet = load_wallet_balance()

    now_utc = datetime.now(timezone.utc)
    et_offset = timedelta(hours=-4)
    now_et = now_utc + et_offset
    now = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    now_et_str = now_et.strftime("%H:%M ET")
    log_escaped = log_tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Market session indicator
    h = now_utc.hour
    if 0 <= h < 3:
        session_name = "Asia Session (Tokyo)"
        session_color = "#d29922"
    elif 3 <= h < 7:
        session_name = "Dead Zone - Low Vol"
        session_color = "#8b949e"
    elif 7 <= h < 8:
        session_name = "Asia/Europe Overlap"
        session_color = "#d29922"
    elif 8 <= h < 12:
        session_name = "EU Session"
        session_color = "#58a6ff"
    elif 12 <= h < 14:
        session_name = "US Pre-Market"
        session_color = "#d29922"
    elif 14 <= h < 21:
        session_name = "US Session - Peak Vol"
        session_color = "#3fb950"
    elif 21 <= h < 23:
        session_name = "US Close / After-Hours"
        session_color = "#d29922"
    else:
        session_name = "Late Night - Low Activity"
        session_color = "#8b949e"

    a = mm["all"]
    t = mm["today"]

    today_pnl_color = "#3fb950" if t["total_pnl"] >= 0 else "#f85149"
    all_pnl_color = "#3fb950" if a["total_pnl"] >= 0 else "#f85149"

    # Compute today's hourly rate
    today_first_ts = None
    for tr in mm.get("mm_trades", []):
        if tr.get("timestamp", "").startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d")):
            today_first_ts = tr.get("timestamp")
            break
    if today_first_ts and t["total_pairs"] > 0:
        try:
            first_dt = datetime.fromisoformat(today_first_ts)
            hours_active = max((datetime.now(timezone.utc) - first_dt).total_seconds() / 3600, 0.1)
            hourly_rate = t["total_pnl"] / hours_active
        except (ValueError, TypeError):
            hours_active = 0
            hourly_rate = 0
    else:
        hours_active = 0
        hourly_rate = 0
    hourly_color = "#3fb950" if hourly_rate >= 0 else "#f85149"
    fill_rate_color = "#3fb950" if a["fill_rate"] >= 0.5 else "#d29922" if a["fill_rate"] >= 0.2 else "#f85149"
    # Avg profit per locked pair
    avg_profit_per_pair = (a["real_pair_profit"] / a["locked_pairs"]) if a["locked_pairs"] > 0 else 0
    avg_profit_color = "#3fb950" if avg_profit_per_pair > 0 else "#d29922"

    # Build MM trades table rows
    mm_trades = mm.get("mm_trades", [])
    rows = ""
    for tr in reversed(mm_trades[-50:]):
        ts = tr.get("timestamp", "")[:19]
        slug = tr.get("slug", "")
        short_slug = slug.replace("btc-updown-5m-", "5m-") if slug else ""
        direction = tr.get("direction", "?").upper()
        price = tr.get("entry_price", 0)
        mode = tr.get("mode", "")
        pc = _extract_pair_cost(tr)
        pc_str = f"${pc:.2f}" if pc is not None else "-"
        spread_str = f"${1.00 - pc:.3f}" if pc is not None else "-"
        hedged = tr.get("hedged", False)

        # Mode badge
        if mode == "mm-pair-arb":
            mode_text = '<span style="color:#3fb950;font-size:10px;font-weight:600">ARB</span>'
        elif mode == "mm-pair-complete":
            mode_text = '<span style="color:#58a6ff;font-size:10px;font-weight:600">COMPLETE</span>'
        elif mode == "mm-pair-offhours":
            mode_text = '<span style="color:#d29922;font-size:10px;font-weight:600">MM-OFF</span>'
        else:
            mode_text = '<span style="color:#8b949e;font-size:10px;font-weight:600">MM</span>'

        # Status
        if hedged:
            status = '<span style="color:#3fb950;font-weight:600">PAIRED</span>'
        elif mode == "mm-pair-arb":
            status = '<span style="color:#3fb950;font-weight:600">ARB</span>'
        elif tr.get("resolved") is not None:
            won = tr.get("won", False)
            pnl = tr.get("pnl", 0)
            status = f'<span style="color:{"#3fb950" if won else "#f85149"};font-weight:600">{"WIN" if won else "LOSS"} ${pnl:+.2f}</span>'
        else:
            status = '<span class="muted">pending</span>'

        rows += f"""<tr>
          <td class="muted">{ts}</td>
          <td>{mode_text}</td>
          <td>{short_slug}</td>
          <td class="{'up' if direction == 'UP' else 'down'}">{direction}</td>
          <td>${price:.2f}</td>
          <td>{pc_str}</td>
          <td>{spread_str}</td>
          <td>{status}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>PolyBMiCoB MM</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace;
         background:#0d1117; color:#c9d1d9; padding:20px; max-width:1200px; margin:0 auto; }}
  h1 {{ color:#58a6ff; font-size:22px; margin-bottom:4px; }}
  .sub {{ color:#8b949e; font-size:13px; margin-bottom:24px; }}
  .hero {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));
           gap:16px; margin-bottom:28px; }}
  .hero-card {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
                padding:24px; text-align:center; }}
  .hero-card .num {{ font-size:42px; font-weight:800; line-height:1.1; }}
  .hero-card .lbl {{ font-size:13px; color:#8b949e; margin-top:6px; }}
  .hero-card .detail {{ font-size:11px; color:#6e7681; margin-top:4px; }}
  .stats-row {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:24px; }}
  .stat {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
           padding:12px 18px; text-align:center; flex:1; min-width:120px; }}
  .stat .val {{ font-size:20px; font-weight:700; color:#f0f6fc; }}
  .stat .lbl {{ font-size:10px; color:#8b949e; margin-top:2px; text-transform:uppercase; letter-spacing:0.5px; }}
  details {{ margin-bottom:16px; }}
  summary {{ cursor:pointer; background:#161b22; border:1px solid #30363d; border-radius:8px;
             padding:12px 16px; font-size:14px; color:#58a6ff; font-weight:600;
             list-style:none; display:flex; align-items:center; gap:8px; }}
  summary::-webkit-details-marker {{ display:none; }}
  summary::before {{ content:">>"; font-size:12px; transition:transform 0.2s; }}
  details[open] summary::before {{ transform:rotate(90deg); }}
  details[open] summary {{ border-radius:8px 8px 0 0; }}
  .expand-content {{ background:#161b22; border:1px solid #30363d; border-top:none;
                     border-radius:0 0 8px 8px; padding:0; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ background:#21262d; color:#8b949e; font-size:11px; text-transform:uppercase;
       padding:8px; text-align:left; }}
  td {{ padding:7px 8px; border-top:1px solid #21262d; font-size:12px; }}
  tr:hover {{ background:#1c2128; }}
  .up {{ color:#3fb950; font-weight:bold; }}
  .down {{ color:#f85149; font-weight:bold; }}
  .muted {{ color:#8b949e; font-size:11px; }}
  pre {{ font-size:11px; color:#8b949e; overflow-x:auto; white-space:pre-wrap;
         word-wrap:break-word; line-height:1.5; max-height:500px; overflow-y:auto; padding:12px; }}
  .empty {{ text-align:center; padding:40px; color:#8b949e; }}
  .mm-badge {{ display:inline-block; background:#1f6feb22; border:1px solid #1f6feb; border-radius:4px;
               padding:2px 8px; font-size:11px; color:#58a6ff; font-weight:600; margin-left:8px; }}
</style>
</head>
<body>

<h1>PolyBMiCoB <span class="mm-badge">MM PAIR MODE</span></h1>
<p class="sub">Market Maker 24/7 -- {now} / {now_et_str} / <span style="color:{session_color};font-weight:600">{session_name}</span></p>

<div class="hero">
  <div class="hero-card">
    <div class="num" style="color:#d29922">${wallet['usdc_balance']:.2f}</div>
    <div class="lbl">Wallet USDC</div>
    <div class="detail">{'updated ' + wallet['updated_at'][11:16] + ' UTC' if wallet['updated_at'] else 'waiting for first check'}</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{today_pnl_color}">${t['total_pnl']:+.2f}</div>
    <div class="lbl">Today Real P&L</div>
    <div class="detail">locked ${t['real_pair_profit']:+.2f} + partial ${t['partial_pnl']:+.2f} | <span style="color:{hourly_color}">${hourly_rate:+.2f}/h</span></div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{'#3fb950' if t['real_pair_profit'] >= 0 else '#f85149'}">${t['real_pair_profit']:+.2f}</div>
    <div class="lbl">Locked Pair Profit</div>
    <div class="detail">{t['locked_pairs']} locked / {t['total_pairs']} pairs today ({hours_active:.1f}h)</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{all_pnl_color}">${a['total_pnl']:+.2f}</div>
    <div class="lbl">All-Time MM P&L</div>
    <div class="detail">locked ${a['real_pair_profit']:+.2f} + partial ${a['partial_pnl']:+.2f}</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{fill_rate_color}">{a['fill_rate']:.0%}</div>
    <div class="lbl">Lock Rate</div>
    <div class="detail">{a['locked_pairs']} locked / {a['total_pairs']} total</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{avg_profit_color}">${avg_profit_per_pair:.2f}</div>
    <div class="lbl">Avg Profit/Pair</div>
    <div class="detail">avg cost ${a['avg_pair_cost']:.3f} | {mm['pending']} pending</div>
  </div>
</div>

<div class="stats-row">
  <div class="stat">
    <div class="val" style="color:#58a6ff">{a['total_pairs']}</div>
    <div class="lbl">Total Pairs</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#3fb950">{a['both_filled']}</div>
    <div class="lbl">Both-Filled (Arb)</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#58a6ff">{a['completed']}</div>
    <div class="lbl">Completed In-Play</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#d29922">{a['partials']}</div>
    <div class="lbl">Partials (1-side)</div>
  </div>
  <div class="stat">
    <div class="val">{mm['total_mm_trades']}</div>
    <div class="lbl">Total MM Trades</div>
  </div>
  <div class="stat">
    <div class="val">{mm['pending']}</div>
    <div class="lbl">Pending</div>
  </div>
</div>

<div class="stats-row">
  <div class="stat">
    <div class="val" style="color:#58a6ff">{t['total_pairs']}</div>
    <div class="lbl">Pairs Today</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#3fb950">{t['both_filled']}</div>
    <div class="lbl">Arb Today</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#58a6ff">{t['completed']}</div>
    <div class="lbl">Completed Today</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#d29922">{t['partials']}</div>
    <div class="lbl">Partials Today</div>
  </div>
  <div class="stat">
    <div class="val">${t['avg_pair_cost']:.3f}</div>
    <div class="lbl">Avg Cost Today</div>
  </div>
  <div class="stat">
    <div class="val" style="color:{'#3fb950' if t['real_pair_profit'] > 0 else '#d29922'}">${(t['real_pair_profit'] / t['locked_pairs'] if t['locked_pairs'] > 0 else 0):.2f}</div>
    <div class="lbl">Avg Profit Today</div>
  </div>
</div>

{render_mm_charts(mm)}

{'<div class="empty">No MM trades yet. Bot is running in MM pair mode 24/7.</div>' if not mm_trades else f"""
<details open>
  <summary>Today Pair Flow ({t['total_pairs']} pairs: {t['locked_pairs']} locked, {t['partials']} partial)</summary>
  <div class="expand-content">
    <table>
    <thead>
      <tr><th>Time</th><th>Market</th><th>1st Side</th><th>Price</th><th>Locked?</th><th>Lock Type</th><th>Pair Cost</th><th>Spread</th><th>~Shares</th><th>Profit</th><th>Status</th></tr>
    </thead>
    <tbody>
    {''.join(_render_pair_row(p) for p in reversed(t.get('pair_details', []))) if t.get('pair_details') else '<tr><td colspan="11" class="muted" style="text-align:center;padding:20px;">No pairs today yet</td></tr>'}
    </tbody>
    </table>
  </div>
</details>

<details>
  <summary>Recent MM Pairs ({min(len(mm_trades), 50)} of {len(mm_trades)})</summary>
  <div class="expand-content">
    <table>
    <thead>
      <tr><th>Time</th><th>Mode</th><th>Market</th><th>Side</th><th>Price</th><th>Pair Cost</th><th>Spread</th><th>Status</th></tr>
    </thead>
    <tbody>
    {rows}
    </tbody>
    </table>
  </div>
</details>
"""}

<details>
  <summary>Bot Log (last 40 lines)</summary>
  <div class="expand-content">
    <pre>{log_escaped}</pre>
  </div>
</details>

</body>
</html>"""


def render_html() -> str:
    trades = load_trades()
    stats = compute_stats(trades)
    log_tail = get_log_tail(40)
    activity = load_today_stats()
    wallet = load_wallet_balance()

    # Streak display
    streak = stats["streak"]
    if streak > 0:
        streak_text = f"+{streak}W"
        streak_color = "#3fb950"
    elif streak < 0:
        streak_text = f"{streak}L"
        streak_color = "#f85149"
    else:
        streak_text = "0"
        streak_color = "#8b949e"

    pnl_color = "#3fb950" if stats["total_pnl"] >= 0 else "#f85149"
    wr_color = "#3fb950" if stats["win_rate"] >= 0.5 else "#f85149"

    # Daily P&L from today's resolved trades
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_resolved = [
        t for t in trades
        if t.get("timestamp", "").startswith(today_str)
        and t.get("resolved") is not None
        and not t.get("dry_run", True)
    ]
    daily_pnl = sum(t.get("pnl", 0) for t in today_resolved)
    daily_wins = sum(1 for t in today_resolved if t.get("won"))
    daily_losses = sum(1 for t in today_resolved if not t.get("won"))
    daily_pnl_color = "#3fb950" if daily_pnl >= 0 else "#f85149"

    # UP/DOWN breakdown
    up_total = stats["up_wins"] + stats["up_losses"]
    down_total = stats["down_wins"] + stats["down_losses"]
    up_wr = f'{stats["up_wins"]}/{up_total} ({stats["up_wins"]/up_total:.0%})' if up_total > 0 else "n/a"
    down_wr = f'{stats["down_wins"]}/{down_total} ({stats["down_wins"]/down_total:.0%})' if down_total > 0 else "n/a"

    # Build trades rows
    rows = ""
    for t in reversed(trades[-50:]):
        ts = t.get("timestamp", "")[:19]
        slug = t.get("slug", "")
        short_slug = slug.replace("btc-updown-5m-", "5m-") if slug else ""
        direction = t.get("direction", "?").upper()
        dir_class = "up" if direction == "UP" else "down"
        price = t.get("entry_price", 0)
        edge = t.get("edge", 0) * 100
        btc = t.get("btc_price", 0)
        mom = t.get("momentum", 0)
        dry = " (dry)" if t.get("dry_run") else ""

        # Trade mode
        mode = t.get("mode", "pre-market")
        if mode == "in-play":
            mode_text = '<span class="mode-ip">IN-PLAY</span>'
        else:
            mode_text = '<span class="mode-pre">PRE</span>'

        if t.get("resolved") is not None:
            won = t.get("won", False)
            pnl = t.get("pnl", 0)
            result_text = f'<span class="{"up" if won else "down"}">{"WIN" if won else "LOSS"}</span>'
            pnl_text = f'<span class="{"up" if pnl >= 0 else "down"}">${pnl:+.2f}</span>'
        else:
            result_text = '<span class="muted">...</span>'
            pnl_text = '<span class="muted">-</span>'

        rows += f"""<tr>
          <td class="muted">{ts}</td>
          <td>{mode_text}</td>
          <td>{short_slug}</td>
          <td class="{dir_class}">{direction}{dry}</td>
          <td>${price:.2f}</td>
          <td>{edge:.1f}%</td>
          <td>{result_text}</td>
          <td>{pnl_text}</td>
          <td>${btc:,.0f}</td>
          <td>{mom:+.3f}%</td>
        </tr>"""

    now_utc = datetime.now(timezone.utc)
    et_offset = timedelta(hours=-4)  # EDT (Mar-Nov)
    now_et = now_utc + et_offset
    now = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    now_et_str = now_et.strftime("%H:%M ET")
    log_escaped = log_tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # NOTRADING indicator: show when current hour is outside TRADING_HOURS_UTC
    notrading_suffix = ""
    if TRADING_HOURS is not None and now_utc.hour not in TRADING_HOURS:
        notrading_suffix = ' / <span style="color:#f85149;font-weight:700">NOTRADING</span>'

    # MAXLOSS indicator: show when daily loss limit reached
    max_daily_loss = float(os.environ.get("MAX_DAILY_LOSS_USD", "20.00"))
    daily_loss_total = sum(
        abs(t.get("pnl", 0)) for t in today_resolved if not t.get("won", False)
    )
    maxloss_suffix = ""
    if daily_loss_total >= max_daily_loss:
        maxloss_suffix = (
            f' / <span style="color:#f85149;font-weight:700">'
            f'MAXLOSS ${daily_loss_total:.0f}/${max_daily_loss:.0f}</span>'
        )

    # Market session indicator based on UTC hour
    h = now_utc.hour
    if 0 <= h < 3:
        session_name = "\u2197 Asia Session (Tokyo)"
        session_color = "#d29922"
    elif 3 <= h < 7:
        session_name = "\u22ef Dead Zone - Low Volatility"
        session_color = "#8b949e"
    elif 7 <= h < 8:
        session_name = "\u2192 Asia/Europe Overlap"
        session_color = "#d29922"
    elif 8 <= h < 12:
        session_name = "\u2197 EU Session"
        session_color = "#58a6ff"
    elif 12 <= h < 14:
        session_name = "\u2192 US Pre-Market"
        session_color = "#d29922"
    elif 14 <= h < 21:
        session_name = "\u2197 US Session - Peak Volatility"
        session_color = "#3fb950"
    elif 21 <= h < 23:
        session_name = "\u2198 US Close / After-Hours"
        session_color = "#d29922"
    else:
        session_name = "\u22ef Late Night - Low Activity"
        session_color = "#8b949e"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>PolyBMiCoB</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace;
         background:#0d1117; color:#c9d1d9; padding:20px; max-width:1200px; margin:0 auto; }}
  h1 {{ color:#58a6ff; font-size:22px; margin-bottom:4px; }}
  .sub {{ color:#8b949e; font-size:13px; margin-bottom:24px; }}

  /* Hero cards */
  .hero {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));
           gap:16px; margin-bottom:28px; }}
  .hero-card {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
                padding:24px; text-align:center; }}
  .hero-card .num {{ font-size:42px; font-weight:800; line-height:1.1; }}
  .hero-card .lbl {{ font-size:13px; color:#8b949e; margin-top:6px; }}
  .hero-card .detail {{ font-size:11px; color:#6e7681; margin-top:4px; }}

  /* Secondary stats */
  .stats-row {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:24px; }}
  .stat {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
           padding:12px 18px; text-align:center; flex:1; min-width:120px; }}
  .stat .val {{ font-size:20px; font-weight:700; color:#f0f6fc; }}
  .stat .lbl {{ font-size:10px; color:#8b949e; margin-top:2px; text-transform:uppercase; letter-spacing:0.5px; }}

  /* Expander */
  details {{ margin-bottom:16px; }}
  summary {{ cursor:pointer; background:#161b22; border:1px solid #30363d; border-radius:8px;
             padding:12px 16px; font-size:14px; color:#58a6ff; font-weight:600;
             list-style:none; display:flex; align-items:center; gap:8px; }}
  summary::-webkit-details-marker {{ display:none; }}
  summary::before {{ content:">>"; font-size:12px; transition:transform 0.2s; }}
  details[open] summary::before {{ transform:rotate(90deg); }}
  details[open] summary {{ border-radius:8px 8px 0 0; }}
  .expand-content {{ background:#161b22; border:1px solid #30363d; border-top:none;
                     border-radius:0 0 8px 8px; padding:0; }}

  /* Table */
  table {{ width:100%; border-collapse:collapse; }}
  th {{ background:#21262d; color:#8b949e; font-size:11px; text-transform:uppercase;
       padding:8px; text-align:left; }}
  td {{ padding:7px 8px; border-top:1px solid #21262d; font-size:12px; }}
  tr:hover {{ background:#1c2128; }}
  .up {{ color:#3fb950; font-weight:bold; }}
  .down {{ color:#f85149; font-weight:bold; }}
  .muted {{ color:#8b949e; font-size:11px; }}
  .mode-pre {{ color:#58a6ff; font-size:10px; font-weight:600; }}
  .mode-ip {{ color:#d29922; font-size:10px; font-weight:600; }}

  /* Log */
  pre {{ font-size:11px; color:#8b949e; overflow-x:auto; white-space:pre-wrap;
         word-wrap:break-word; line-height:1.5; max-height:500px; overflow-y:auto; padding:12px; }}

  .empty {{ text-align:center; padding:40px; color:#8b949e; }}
</style>
</head>
<body>

<h1>PolyBMiCoB</h1>
<p class="sub">BTC 5-Min Micro-Cycle Options Bot -- {now} / {now_et_str} / <span style="color:{session_color};font-weight:600">{session_name}</span>{notrading_suffix}{maxloss_suffix}</p>

<div class="hero">
  <div class="hero-card">
    <div class="num" style="color:#d29922">${wallet['usdc_balance']:.2f}</div>
    <div class="lbl">Wallet USDC</div>
    <div class="detail">{'updated ' + wallet['updated_at'][11:16] + ' UTC' if wallet['updated_at'] else 'waiting for first check'}</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{daily_pnl_color}">${daily_pnl:+.2f}</div>
    <div class="lbl">Today P&L</div>
    <div class="detail">{daily_wins}W / {daily_losses}L today</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{pnl_color}">${stats['total_pnl']:+.2f}</div>
    <div class="lbl">All-Time P&L</div>
    <div class="detail">${stats['total_invested']:.2f} invested</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{wr_color}">{stats['win_rate']:.0%}</div>
    <div class="lbl">Win Rate</div>
    <div class="detail">{stats['wins']}W / {stats['losses']}L ({stats['resolved']} resolved)</div>
  </div>
  <div class="hero-card">
    <div class="num" style="color:{streak_color}">{streak_text}</div>
    <div class="lbl">Streak</div>
    <div class="detail">{stats['unresolved']} pending</div>
  </div>
</div>

<div class="stats-row">
  <div class="stat">
    <div class="val" style="color:#58a6ff">{stats['live_trades']}</div>
    <div class="lbl">Total Trades</div>
  </div>
  <div class="stat">
    <div class="val">{up_wr}</div>
    <div class="lbl">UP Win Rate</div>
  </div>
  <div class="stat">
    <div class="val">{down_wr}</div>
    <div class="lbl">DOWN Win Rate</div>
  </div>
  <div class="stat">
    <div class="val">{stats['avg_confidence']:.0%}</div>
    <div class="lbl">Avg Confidence</div>
  </div>
  <div class="stat">
    <div class="val">{stats['avg_edge']:.1%}</div>
    <div class="lbl">Avg Edge</div>
  </div>
</div>

<div class="stats-row">
  <div class="stat">
    <div class="val">{activity['total_cycles']}</div>
    <div class="lbl">Cycles Today</div>
  </div>
  <div class="stat">
    <div class="val" style="color:{'#f85149' if activity['momentum_skips'] > activity['total_cycles'] * 0.5 else '#8b949e'}">{activity['momentum_skips']}</div>
    <div class="lbl">Mom. Skips</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#58a6ff">{activity['pre_signals']}</div>
    <div class="lbl">Pre Signals</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#d29922">{activity['inplay_signals']}</div>
    <div class="lbl">In-Play Signals</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#3fb950">{activity['orders_filled']}</div>
    <div class="lbl">Filled</div>
  </div>
  <div class="stat">
    <div class="val" style="color:{'#f85149' if (activity['orders_not_filled'] + activity['orders_no_liquidity'] + activity['orders_failed']) > 0 else '#8b949e'}">{activity['orders_not_filled'] + activity['orders_no_liquidity'] + activity['orders_failed']}</div>
    <div class="lbl">Rejected</div>
  </div>
  <div class="stat">
    <div class="val">{activity['trades_resolved']}</div>
    <div class="lbl">Resolved</div>
  </div>
</div>

{render_charts(trades)}

{'<div class="empty">No trades yet. Start the bot to see data.</div>' if not trades else f"""
<details>
  <summary>Recent Trades ({min(len(trades), 50)} of {len(trades)})</summary>
  <div class="expand-content">
    <table>
    <thead>
      <tr><th>Time</th><th>Mode</th><th>Market</th><th>Dir</th><th>Price</th><th>Edge</th><th>Result</th><th>P&L</th><th>BTC</th><th>Mom</th></tr>
    </thead>
    <tbody>
    {rows}
    </tbody>
    </table>
  </div>
</details>
"""}

<details>
  <summary>Hourly Breakdown ({activity['today']})</summary>
  <div class="expand-content" style="padding:16px;">
    <div style="font-size:12px; color:#8b949e; margin-bottom:8px;">
      Last signal: <span style="color:#c9d1d9">{activity['last_signal_time'] or 'none'}</span>
      &nbsp;&nbsp;|&nbsp;&nbsp;
      Last order: <span style="color:#c9d1d9">{activity['last_order_time'] or 'none'}</span>
      {'&nbsp;&nbsp;|&nbsp;&nbsp;<span style="color:#f85149">Failed: ' + ', '.join(set(activity['fail_reasons'])) + '</span>' if activity['fail_reasons'] else ''}
    </div>
    <table>
    <thead>
      <tr><th>Hour (UTC)</th><th>Cycles</th><th>Mom. Skip</th><th>Skip %</th><th>Signals</th><th>Filled</th><th>Rejected</th></tr>
    </thead>
    <tbody>
    {''.join(f"""<tr>
      <td>{h}:00</td>
      <td>{d['cycles']}</td>
      <td style="color:{'#f85149' if d['skips'] > d['cycles'] * 0.8 else '#8b949e'}">{d['skips']}</td>
      <td style="color:{'#f85149' if d['skips'] > d['cycles'] * 0.8 else '#8b949e'}">{d['skips']*100//d['cycles'] if d['cycles'] > 0 else 0}%</td>
      <td style="color:{'#58a6ff' if d['signals'] > 0 else '#8b949e'}">{d['signals']}</td>
      <td style="color:{'#3fb950' if d['filled'] > 0 else '#8b949e'}">{d['filled']}</td>
      <td style="color:{'#f85149' if d['rejected'] > 0 else '#8b949e'}">{d['rejected']}</td>
    </tr>""" for h, d in sorted(activity['hourly'].items()))}
    </tbody>
    </table>
  </div>
</details>

<details>
  <summary>In-Play Signal Analysis ({len(activity['inplay_events'])} signals, {sum(1 for e in activity['inplay_events'] if e['outcome'] == 'filled')} filled, {sum(1 for e in activity['inplay_events'] if e['outcome'] not in ('filled', 'unknown', 'pending'))} rejected)</summary>
  <div class="expand-content" style="padding:16px;">
    <div style="font-size:12px; color:#8b949e; margin-bottom:12px;">
      {''.join(f'<span style="display:inline-block;background:#21262d;border-radius:4px;padding:4px 10px;margin:0 6px 6px 0;font-size:11px;"><span style="color:#c9d1d9">{r}</span> <span style="color:#f85149;font-weight:700">{activity["fail_reasons"].count(r)}</span></span>' for r in sorted(set(activity['fail_reasons'])))}
    </div>
    <table>
    <thead>
      <tr><th>Time</th><th>Dir</th><th>Market</th><th>Edge</th><th>BTC Move</th><th>Elapsed</th><th>Outcome</th><th>Detail</th></tr>
    </thead>
    <tbody>
    {''.join(f"""<tr>
      <td class="muted">{e['time'][11:]}</td>
      <td class="{'up' if e['dir'] == 'UP' else 'down'}">{e['dir']}</td>
      <td>{e['market']}</td>
      <td>{e['edge']}</td>
      <td>{e['btc_move']}</td>
      <td class="muted">{e['elapsed']}</td>
      <td style="color:{'#3fb950' if e['outcome'] == 'filled' else '#f85149' if e['outcome'] not in ('unknown', 'pending') else '#8b949e'};font-weight:600">{e['outcome']}</td>
      <td class="muted">{e.get('detail', '')}</td>
    </tr>""" for e in reversed(activity['inplay_events'])) if activity['inplay_events'] else '<tr><td colspan="8" class="muted" style="text-align:center;padding:20px;">No in-play signals today</td></tr>'}
    </tbody>
    </table>
  </div>
</details>

<details>
  <summary>Bot Log (last 40 lines)</summary>
  <div class="expand-content">
    <pre>{log_escaped}</pre>
  </div>
</details>

</body>
</html>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = render_mm_html() if MM_PAIR_ENABLED else render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/trades":
            trades = load_trades()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(trades, indent=2).encode("utf-8"))
        elif self.path == "/api/stats":
            trades = load_trades()
            stats = compute_stats(trades)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats, indent=2).encode("utf-8"))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])
    print(f"Dashboard running at http://0.0.0.0:{port}")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
