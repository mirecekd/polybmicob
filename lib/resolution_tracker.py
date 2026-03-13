"""
Resolution tracker for PolyBMiCoB trades.

Checks resolved BTC 5m markets via CLOB API and updates trade records
with win/loss status and actual P&L.

Resolution logic:
  1. For each unresolved trade, query CLOB /markets/{conditionId}
  2. If market.closed=True, check tokens[].winner
  3. If our token_id matches a winner token -> WIN, else LOSS
  4. P&L = (1.00 - entry_price) if WIN, or (-entry_price) if LOSS

CLOB response structure for resolved market:
  {
    "closed": true,
    "tokens": [
      {"token_id": "...", "outcome": "Up", "winner": true, "price": 1.0},
      {"token_id": "...", "outcome": "Down", "winner": false, "price": 0.0}
    ]
  }
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("polybmicob.resolver")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass
class ResolutionStats:
    """Aggregated resolution statistics."""

    total_trades: int = 0
    resolved: int = 0
    unresolved: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    up_wins: int = 0
    up_losses: int = 0
    down_wins: int = 0
    down_losses: int = 0
    streak: int = 0  # positive = win streak, negative = loss streak


def _get_condition_id_from_slug(slug: str) -> str:
    """Look up conditionId from Gamma API by event slug."""
    try:
        resp = httpx.get(
            f"{GAMMA_API_BASE}/events",
            params={"slug": slug},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and data[0].get("markets"):
            return data[0]["markets"][0].get("conditionId", "")
    except Exception as exc:
        log.debug("Failed to get conditionId for %s: %s", slug, exc)
    return ""


def _check_market_resolution(condition_id: str) -> dict | None:
    """
    Check market resolution via CLOB API.

    Returns dict with:
      - closed: bool
      - winner_outcome: str ("Up" or "Down") or None
      - winner_token_id: str or None
    """
    try:
        resp = httpx.get(
            f"{CLOB_API_BASE}/markets/{condition_id}",
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

        if not data.get("closed"):
            return None  # Not resolved yet

        tokens = data.get("tokens", [])
        winner_outcome = None
        winner_token_id = None

        for token in tokens:
            if token.get("winner") is True:
                winner_outcome = token.get("outcome", "")
                winner_token_id = token.get("token_id", "")
                break

        return {
            "closed": True,
            "winner_outcome": winner_outcome,
            "winner_token_id": winner_token_id,
        }
    except Exception as exc:
        log.debug("Failed to check resolution for %s: %s", condition_id, exc)
        return None


def resolve_trades(
    trades_file: Path,
    rate_limit_sec: float = 0.5,
) -> int:
    """
    Check and update resolution status for all unresolved trades.

    Args:
        trades_file: Path to btc_trades.json
        rate_limit_sec: Seconds between API calls

    Returns:
        Number of newly resolved trades
    """
    if not trades_file.exists():
        return 0

    try:
        trades = json.loads(trades_file.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    newly_resolved = 0
    changed = False

    for trade in trades:
        # Skip already resolved trades
        if trade.get("resolved") is not None:
            continue

        # Skip dry-run trades
        if trade.get("dry_run", True):
            continue

        slug = trade.get("slug", "")
        if not slug:
            continue

        # Get conditionId (from trade or look up)
        condition_id = trade.get("condition_id", "")
        if not condition_id:
            condition_id = _get_condition_id_from_slug(slug)
            if condition_id:
                trade["condition_id"] = condition_id
                changed = True
            else:
                continue
            time.sleep(rate_limit_sec)

        # Check resolution
        resolution = _check_market_resolution(condition_id)
        time.sleep(rate_limit_sec)

        if resolution is None:
            continue  # Not resolved yet or API error

        # Determine win/loss
        our_token_id = trade.get("token_id", "")
        our_direction = trade.get("direction", "")
        winner_outcome = resolution.get("winner_outcome", "")
        winner_token_id = resolution.get("winner_token_id", "")

        # Match by token_id (most reliable) or by direction
        if winner_token_id and our_token_id:
            won = our_token_id == winner_token_id
        else:
            won = our_direction.lower() == winner_outcome.lower()

        entry_price = trade.get("entry_price", 0.5)
        pnl = (1.0 - entry_price) if won else (-entry_price)

        trade["resolved"] = True
        trade["won"] = won
        trade["pnl"] = round(pnl, 4)
        trade["winner_outcome"] = winner_outcome

        newly_resolved += 1
        changed = True

        log.info(
            "  %s: %s (bet %s @ $%.2f -> %s $%+.2f)",
            slug,
            "WIN" if won else "LOSS",
            our_direction.upper(),
            entry_price,
            winner_outcome.upper(),
            pnl,
        )

    # Save updated trades
    if changed:
        tmp = trades_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(trades, indent=2))
        tmp.rename(trades_file)

    return newly_resolved


def compute_resolution_stats(trades_file: Path) -> ResolutionStats:
    """Compute win/loss statistics from trade history."""
    stats = ResolutionStats()

    if not trades_file.exists():
        return stats

    try:
        trades = json.loads(trades_file.read_text())
    except (json.JSONDecodeError, OSError):
        return stats

    # Only count live trades
    live_trades = [t for t in trades if not t.get("dry_run", True)]
    stats.total_trades = len(live_trades)

    resolved = [t for t in live_trades if t.get("resolved") is not None]
    stats.resolved = len(resolved)
    stats.unresolved = stats.total_trades - stats.resolved

    for t in resolved:
        won = t.get("won", False)
        direction = t.get("direction", "")
        pnl = t.get("pnl", 0)

        stats.total_pnl += pnl

        if won:
            stats.wins += 1
            if direction == "up":
                stats.up_wins += 1
            else:
                stats.down_wins += 1
        else:
            stats.losses += 1
            if direction == "up":
                stats.up_losses += 1
            else:
                stats.down_losses += 1

    if stats.resolved > 0:
        stats.win_rate = stats.wins / stats.resolved

    # Calculate current streak
    streak = 0
    for t in reversed(resolved):
        won = t.get("won", False)
        if streak == 0:
            streak = 1 if won else -1
        elif won and streak > 0:
            streak += 1
        elif not won and streak < 0:
            streak -= 1
        else:
            break
    stats.streak = streak

    return stats
