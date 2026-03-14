#!/usr/bin/env python3
"""
PolyBMiCoB Trade Dashboard

Simple HTTP server with big performance cards and collapsible details.

Usage:
  python web/dashboard.py [--port 8005]
"""

import json
import os
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.resolution_tracker import compute_resolution_stats

DATA_DIR = Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "btc_trades.json"
LOG_FILE = DATA_DIR / "btc_bot.log"
PORT = int(os.environ.get("DASHBOARD_PORT", "8005"))


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
        "orders_placed": 0,
        "orders_failed": 0,
        "trades_resolved": 0,
        "hourly": {},  # hour -> {cycles, skips, signals, orders}
        "last_order_time": None,
        "last_signal_time": None,
        "fail_reasons": [],
    }

    if not LOG_FILE.exists():
        return result

    try:
        text = LOG_FILE.read_text()
    except OSError:
        return result

    for line in text.split("\n"):
        if not line.startswith(today):
            continue

        # Extract hour
        try:
            hour = line[11:13]
        except IndexError:
            continue

        if hour not in result["hourly"]:
            result["hourly"][hour] = {"cycles": 0, "skips": 0, "signals": 0, "orders": 0}

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
            result["inplay_signals"] += 1
            result["hourly"][hour]["signals"] += 1
            result["last_signal_time"] = line[:19]

        elif "ORDER PLACED:" in line:
            result["orders_placed"] += 1
            result["hourly"][hour]["orders"] += 1
            result["last_order_time"] = line[:19]

        elif "Trade failed:" in line:
            result["orders_failed"] += 1
            # Extract short reason
            if "minimum:" in line:
                result["fail_reasons"].append("min_size")
            elif "balance" in line.lower():
                result["fail_reasons"].append("balance")
            else:
                result["fail_reasons"].append("other")

        elif "Resolved" in line and "trade(s)" in line:
            result["trades_resolved"] += 1

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


def render_html() -> str:
    trades = load_trades()
    stats = compute_stats(trades)
    log_tail = get_log_tail(40)
    activity = parse_today_activity()

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

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log_escaped = log_tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
<p class="sub">BTC 5-Min Micro-Cycle Options Bot -- {now}</p>

<div class="hero">
  <div class="hero-card">
    <div class="num" style="color:{pnl_color}">${stats['total_pnl']:+.2f}</div>
    <div class="lbl">Real P&L</div>
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
  <div class="hero-card">
    <div class="num" style="color:#58a6ff">{stats['live_trades']}</div>
    <div class="lbl">Total Trades</div>
    <div class="detail">{stats['avg_edge']:.1%} avg edge</div>
  </div>
</div>

<div class="stats-row">
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
</div>

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
  <summary>Today's Activity ({activity['today']})</summary>
  <div class="expand-content" style="padding:16px;">
    <div class="stats-row" style="margin-bottom:16px;">
      <div class="stat">
        <div class="val">{activity['total_cycles']}</div>
        <div class="lbl">Cycles</div>
      </div>
      <div class="stat">
        <div class="val" style="color:{'#f85149' if activity['momentum_skips'] > activity['total_cycles'] * 0.5 else '#8b949e'}">{activity['momentum_skips']}</div>
        <div class="lbl">Momentum Skips</div>
      </div>
      <div class="stat">
        <div class="val">{activity['no_signal']}</div>
        <div class="lbl">No Signal</div>
      </div>
      <div class="stat">
        <div class="val" style="color:#58a6ff">{activity['pre_signals']}</div>
        <div class="lbl">Pre-Market Signals</div>
      </div>
      <div class="stat">
        <div class="val" style="color:#d29922">{activity['inplay_signals']}</div>
        <div class="lbl">In-Play Signals</div>
      </div>
      <div class="stat">
        <div class="val" style="color:#3fb950">{activity['orders_placed']}</div>
        <div class="lbl">Orders Placed</div>
      </div>
      <div class="stat">
        <div class="val" style="color:{'#f85149' if activity['orders_failed'] > 0 else '#8b949e'}">{activity['orders_failed']}</div>
        <div class="lbl">Orders Failed</div>
      </div>
      <div class="stat">
        <div class="val">{activity['trades_resolved']}</div>
        <div class="lbl">Resolved</div>
      </div>
    </div>
    <div style="font-size:12px; color:#8b949e; margin-bottom:8px;">
      Last signal: <span style="color:#c9d1d9">{activity['last_signal_time'] or 'none'}</span>
      &nbsp;&nbsp;|&nbsp;&nbsp;
      Last order: <span style="color:#c9d1d9">{activity['last_order_time'] or 'none'}</span>
      {'&nbsp;&nbsp;|&nbsp;&nbsp;<span style="color:#f85149">Failed: ' + ', '.join(set(activity['fail_reasons'])) + '</span>' if activity['fail_reasons'] else ''}
    </div>
    <table>
    <thead>
      <tr><th>Hour (UTC)</th><th>Cycles</th><th>Mom. Skip</th><th>Skip %</th><th>Signals</th><th>Orders</th></tr>
    </thead>
    <tbody>
    {''.join(f"""<tr>
      <td>{h}:00</td>
      <td>{d['cycles']}</td>
      <td style="color:{'#f85149' if d['skips'] > d['cycles'] * 0.8 else '#8b949e'}">{d['skips']}</td>
      <td style="color:{'#f85149' if d['skips'] > d['cycles'] * 0.8 else '#8b949e'}">{d['skips']*100//d['cycles'] if d['cycles'] > 0 else 0}%</td>
      <td style="color:{'#58a6ff' if d['signals'] > 0 else '#8b949e'}">{d['signals']}</td>
      <td style="color:{'#3fb950' if d['orders'] > 0 else '#8b949e'}">{d['orders']}</td>
    </tr>""" for h, d in sorted(activity['hourly'].items()))}
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
            html = render_html()
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
