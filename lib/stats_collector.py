"""
Stats Collector - Incremental JSON statistics for the dashboard.

The bot calls record_*() functions as events happen. These update
data/dashboard_stats.json atomically. The dashboard reads this file
directly instead of parsing the entire log on every refresh.

Stats are keyed by UTC date and reset automatically at midnight.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

STATS_FILE = Path(__file__).parent.parent / "data" / "dashboard_stats.json"


def _load() -> dict:
    """Load current stats from disk."""
    if not STATS_FILE.exists():
        return {}
    try:
        return json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    """Atomic write stats to disk."""
    tmp = STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(STATS_FILE)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_day(data: dict, day: str) -> dict:
    """Ensure the day structure exists in data, return the day dict."""
    if day not in data:
        data[day] = {
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
            "last_order_time": None,
            "last_signal_time": None,
            "fail_reasons": [],
            "inplay_events": [],
            "hourly": {},
        }
    return data[day]


def _ensure_hour(day_data: dict, hour: str) -> dict:
    """Ensure hourly bucket exists."""
    if hour not in day_data["hourly"]:
        day_data["hourly"][hour] = {
            "cycles": 0,
            "skips": 0,
            "signals": 0,
            "filled": 0,
            "rejected": 0,
        }
    return day_data["hourly"][hour]


def _current_hour() -> str:
    return datetime.now(timezone.utc).strftime("%H")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────────────────────────────
# Public API - called from btc_bot.py
# ──────────────────────────────────────────────────────────────


def record_cycle() -> None:
    """Record a scan cycle start."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())
    dd["total_cycles"] += 1
    hh["cycles"] += 1
    _save(data)


def record_momentum_skip() -> None:
    """Record a momentum threshold skip."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())
    dd["momentum_skips"] += 1
    hh["skips"] += 1
    _save(data)


def record_no_signal() -> None:
    """Record that no signal was generated for a market."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    dd["no_signal"] += 1
    _save(data)


def record_pre_signal() -> None:
    """Record a pre-market signal."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())
    dd["pre_signals"] += 1
    hh["signals"] += 1
    dd["last_signal_time"] = _now_ts()
    _save(data)


def record_inplay_signal(
    direction: str,
    market: str,
    edge: str,
    btc_move: str,
    elapsed: str,
) -> None:
    """Record an in-play signal (pending outcome)."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())
    dd["inplay_signals"] += 1
    hh["signals"] += 1
    dd["last_signal_time"] = _now_ts()

    dd["inplay_events"].append({
        "time": _now_ts(),
        "dir": direction.upper(),
        "market": market.replace("btc-updown-5m-", "5m-"),
        "edge": edge,
        "btc_move": btc_move,
        "elapsed": elapsed,
        "outcome": "pending",
        "detail": "",
    })
    _save(data)


def record_order_filled() -> None:
    """Record a filled order."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())
    dd["orders_filled"] += 1
    hh["filled"] += 1
    dd["last_order_time"] = _now_ts()
    _resolve_last_inplay(dd, "filled", "")
    _save(data)


def record_order_rejected(reason: str, detail: str = "") -> None:
    """Record a rejected/failed order."""
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    hh = _ensure_hour(dd, _current_hour())

    if reason == "not_filled":
        dd["orders_not_filled"] += 1
    elif reason in ("no_asks", "low_liq"):
        dd["orders_no_liquidity"] += 1
    else:
        dd["orders_failed"] += 1

    hh["rejected"] += 1
    dd["fail_reasons"].append(reason)
    _resolve_last_inplay(dd, reason, detail)
    _save(data)


def record_resolution(count: int) -> None:
    """Record that trades were resolved."""
    if count <= 0:
        return
    data = _load()
    day = _today_key()
    dd = _ensure_day(data, day)
    dd["trades_resolved"] += count
    _save(data)


def _resolve_last_inplay(dd: dict, outcome: str, detail: str) -> None:
    """Resolve the most recent pending in-play event."""
    for ev in reversed(dd.get("inplay_events", [])):
        if ev.get("outcome") == "pending":
            ev["outcome"] = outcome
            ev["detail"] = detail
            return


# ──────────────────────────────────────────────────────────────
# Read API - called from dashboard
# ──────────────────────────────────────────────────────────────


def load_today_stats() -> dict:
    """Load today's stats for the dashboard. Returns the day dict or empty."""
    data = _load()
    day = _today_key()
    if day not in data:
        return _ensure_day({}, day)[day] if False else {
            "today": day,
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
            "last_order_time": None,
            "last_signal_time": None,
            "fail_reasons": [],
            "inplay_events": [],
            "hourly": {},
        }

    dd = data[day]
    dd["today"] = day
    return dd
