# PolyBMiCoB — Polymarket BTC Micro-Cycle Options Bot

> Development specification for an automated trading bot targeting **BTC 5-minute Up/Down markets** on Polymarket.
> All data verified against live Gamma API and actual polyclaw codebase as of 2026-03-13.

---

## Table of Contents

1. [Target Market Analysis](#1-target-market-analysis)
2. [Existing Polyclaw Infrastructure](#2-existing-polyclaw-infrastructure)
3. [Trading Strategy](#3-trading-strategy)
4. [Implementation Plan](#4-implementation-plan)
5. [Component Specifications](#5-component-specifications)
6. [Risk Management](#6-risk-management)
7. [Quick Start](#7-quick-start)

---

## 1. Target Market Analysis

### 1.1 Market Type: BTC 5-Minute Up/Down

These are **binary prediction markets** where traders bet whether Bitcoin's price will finish **higher or lower** than its opening price over a 5-minute window.

**Example market:** `Bitcoin Up or Down - March 13, 4:20AM-4:25AM ET`
- URL: https://polymarket.com/event/btc-updown-5m-1773390000
- Event ID: `262662`
- Market ID: `1565761`

### 1.2 Market Structure (Verified)

| Field | Value | Notes |
|-------|-------|-------|
| **Slug pattern** | `btc-updown-5m-{UNIX_TIMESTAMP}` | Timestamp = **START** of 5-min window (UTC) |
| **Outcomes** | `["Up", "Down"]` | **NOT** YES/NO — important for token mapping |
| **Up token** | `clobTokenIds[0]` | First token ID = "Up" outcome |
| **Down token** | `clobTokenIds[1]` | Second token ID = "Down" outcome |
| **Resolution** | Chainlink BTC/USD data stream | https://data.chain.link/streams/btc-usd |
| **Resolution rule** | "Up" if `end_price >= start_price`, else "Down" | Greater-than-**or-equal** means ties resolve "Up" |
| **Market interval** | Every 5 minutes (300 seconds) | Timestamps differ by exactly 300 |
| **negRisk** | `false` | Standard market, NOT neg-risk |
| **Duration** | 5 minutes | Window: `[timestamp, timestamp + 300]` |
| **endDate** | `timestamp + 300` (as ISO datetime) | e.g., slug `1773390000` → endDate `2026-03-13T08:25:00Z` |

### 1.3 Slug Timestamp Decoding

```
Slug: btc-updown-5m-1773390000
Timestamp: 1773390000 = 2026-03-13T08:20:00 UTC = 4:20 AM ET
Window: 08:20:00 UTC → 08:25:00 UTC (5 minutes)
endDate in API: 2026-03-13T08:25:00Z
```

**Formula:**
```python
from datetime import datetime, timezone
slug = "btc-updown-5m-1773390000"
start_ts = int(slug.split("-")[-1])  # 1773390000
start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)  # Window start
end_dt = datetime.fromtimestamp(start_ts + 300, tz=timezone.utc)  # Window end
```

### 1.4 Verified Market Data (User's Target Market)

```
conditionId: 0xc2b6155403b8c87a63caaa318f5e8972f4bdf10322bdbd1396be7181f3b9801c
clobTokenIds:
  Up:   58633653608407954532452212095421201412997907783248016455251878750812239806008
  Down: 86920577458407273471718713999417156212567557623180337541346858773375410247047
outcomePrices: ["0.425", "0.575"]  (42.5% Up, 57.5% Down)
volume: $16,865
volume24hr: $17,830
liquidity: $12,027
```

### 1.5 Market Generation Pattern

Markets are pre-generated in batches. Observed pattern:
- New markets appear ~24h before their window
- Created every 5 minutes without gaps
- Also exist for ETH (`eth-updown-5m-*`) and SOL (`sol-updown-5m-*`)
- Markets with no volume yet have `outcomePrices: None`

### 1.6 Gamma API Discovery

```python
# Find upcoming BTC 5m markets
import httpx
resp = httpx.get(
    "https://gamma-api.polymarket.com/events",
    params={
        "limit": 50,
        "active": "true",
        "closed": "false",
        "order": "endDate",
        "ascending": "true",
        "tag": "crypto",
    },
    timeout=30,
)
events = resp.json()
btc_5m = [e for e in events if "btc-updown-5m" in e.get("slug", "")]
```

**Important:** The `slug_contains` parameter on the Gamma API does broad text matching and returns unrelated results. Use `tag=crypto` + client-side slug filtering instead.

Alternatively, search by exact slug:
```python
resp = httpx.get(
    "https://gamma-api.polymarket.com/events",
    params={"slug": "btc-updown-5m-1773390000"},
    timeout=30,
)
```

### 1.7 Resolution Source

**Chainlink BTC/USD Data Stream:** https://data.chain.link/streams/btc-usd

From the market description:
> This market will resolve to "Up" if the Bitcoin price at the end of the time range
> specified in the title is greater than or equal to the price at the beginning of that
> range. Otherwise, it will resolve to "Down".
> The resolution source for this market is information from Chainlink, specifically the
> BTC/USD data stream. Please note that this market is about the price according to
> Chainlink data stream BTC/USD, not according to other sources or spot markets.

---

## 2. Existing Polyclaw Infrastructure

### 2.1 Project Layout

```
/a0/usr/projects/trading/polyclaw/
|-- .env                    # CHAINSTACK_NODE, POLYCLAW_PRIVATE_KEY, OPENROUTER_API_KEY
|-- pyproject.toml          # Python 3.11+, deps: web3, httpx, py-clob-client, eth-account, python-dotenv
|-- lib/
|   |-- __init__.py
|   |-- clob_client.py      # ClobClientWrapper - CLOB trading with proxy/retry support (203 lines)
|   |-- contracts.py         # Contract addresses + ABIs (99 lines)
|   |-- coverage.py          # Hedge portfolio coverage calculations (220 lines)
|   |-- gamma_client.py      # GammaClient - async Gamma API wrapper (182 lines)
|   |-- llm_client.py        # OpenRouter LLM client (171 lines)
|   |-- position_storage.py  # PositionStorage - JSON file with atomic writes (131 lines)
|   |-- wallet_manager.py    # WalletManager - env-based wallet, Web3 balances (167 lines)
|-- scripts/
|   |-- autotrader.py        # Full autotrader: scan/trade/positions/balance (651 lines)
|   |-- autobet_tonight.py   # Auto-scan + auto-trade sports markets (261 lines)
|   |-- execute_trades_v2.py # v2 trade executor - working (226 lines)
|   |-- trade.py             # Split + CLOB sell trade execution (335 lines)
|   |-- monitor_positions.py # Position monitoring + PnL (122 lines)
|   |-- redeem_winnings.py   # On-chain position redemption (151 lines)
|   |-- hedge.py             # LLM-powered hedge discovery (541 lines)
|   |-- markets.py           # Market browsing CLI (205 lines)
|   |-- positions.py         # Position tracking CLI (303 lines)
|   |-- polyclaw.py          # Main CLI entry point (130 lines)
|   |-- wallet.py            # Wallet status CLI (93 lines)
|-- data/
    |-- positions_v2.json    # Main position storage
    |-- positions.json       # v1 positions
```

### 2.2 Wallet Configuration

| Parameter | Value |
|-----------|-------|
| **Proxy wallet (FUNDER)** | `0x146c795a032a01b8f9e872c7680a157eb17e31ee` |
| **Signature type** | `1` (POLY_PROXY for Magic Link accounts) |
| **Chain** | Polygon (chain_id=137) |
| **CLOB endpoint** | `https://clob.polymarket.com` |
| **Gamma API** | `https://gamma-api.polymarket.com` |
| **RPC** | Via `CHAINSTACK_NODE` env var |
| **Private key** | `POLYCLAW_PRIVATE_KEY` env var in `.env` |

### 2.3 Reusable Library Components

#### `lib/gamma_client.py` (182 lines)
- `GammaClient` class with async methods
- `get_trending_markets(limit)` returns `list[Market]`
- `search_markets(query, limit)` returns `list[Market]` (client-side filtering)
- `get_market(market_id)` returns `Market`
- `get_event(event_id)` returns `MarketGroup`
- `Market` dataclass fields: `id, question, slug, condition_id, yes_token_id, no_token_id, yes_price, no_price, volume, volume_24h, liquidity, end_date, active, closed, resolved, outcome`

**NOTE:** The `Market` dataclass uses `yes_token_id`/`no_token_id` naming. For BTC 5m markets, these map to `Up`/`Down` respectively (index 0 = Up = "yes", index 1 = Down = "no").

#### `lib/clob_client.py` (203 lines)
- `ClobClientWrapper` class with retry logic for Cloudflare blocks
- Constructor: `ClobClientWrapper(private_key, address)`
- Has `sell_fok(token_id, amount, price)` method
- Has proxy rotation support via `HTTPS_PROXY` env var
- **KNOWN BUG:** Uses `signature_type=0` hardcoded in `_init_client()` — should be `1` for POLY_PROXY wallets. The scripts that work (autotrader.py, execute_trades_v2.py) bypass this lib and use ClobClient directly with `signature_type=1`.

#### `lib/wallet_manager.py` (167 lines)
- `WalletManager` class — loads key from `POLYCLAW_PRIVATE_KEY` env var
- Properties: `is_unlocked`, `address`
- Methods: `get_balances()` returns `WalletBalances(pol, usdc_e)`, `get_unlocked_key()`
- Uses `CHAINSTACK_NODE` for RPC, bypasses proxy for RPC calls with `proxies={}`

#### `lib/contracts.py` (99 lines)
```python
CONTRACTS = {
    "USDC_E": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "CTF": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "CTF_EXCHANGE": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NEG_RISK_CTF_EXCHANGE": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "NEG_RISK_ADAPTER": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}
```

#### `lib/position_storage.py` (131 lines)
- `PositionStorage` class — JSON file with atomic writes + thread-safe locking
- `PositionEntry` dataclass: `position_id, market_id, question, position, token_id, entry_time, entry_amount, entry_price, split_tx, clob_order_id, clob_filled, status, notes`
- Default path: `~/.openclaw/polyclaw/positions.json`

### 2.4 Proven CLOB Trading Patterns

#### CLOB Connection (CORRECT pattern from autotrader.py)
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=1,   # POLY_PROXY — MUST be 1, not 0
    funder="0x146c795a032a01b8f9e872c7680a157eb17e31ee",
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
```

#### Order Placement (CORRECT pattern from execute_trades_v2.py)
```python
# Get orderbook
book = client.get_order_book(token_id)
asks = sorted(book.asks, key=lambda a: float(a.price)) if book.asks else []
bids = sorted(book.bids, key=lambda b: float(b.price), reverse=True) if book.bids else []

# Calculate execution price (market price + small premium, capped at best ask)
if asks:
    best_ask = float(asks[0].price)
    exec_price = round(min(mkt_price + 0.02, best_ask, 0.95), 2)
else:
    exec_price = round(min(mkt_price + 0.02, 0.95), 2)
exec_price = max(exec_price, 0.02)

# Calculate shares (minimum order value $1.01)
size = round(size_usd / exec_price, 0)
while size * exec_price < 1.01:
    size += 1

# Place GTC limit order
order_args = OrderArgs(price=exec_price, size=size, side=BUY, token_id=token_id)
signed_order = client.create_order(order_args)
result = client.post_order(signed_order, OrderType.GTC)
order_id = result.get("orderID", result.get("id", "unknown"))
```

#### Rate Limiting
- **1.5 seconds** between orders (used in all working scripts)
- Cloudflare may block rapid requests — `lib/clob_client.py` has retry logic with proxy rotation

#### Order Types Used
- **GTC (Good Till Cancelled):** For buy orders — sits in book until filled or cancelled
- **FOK (Fill Or Kill):** For sell orders — immediate fill or reject

### 2.5 Known Issues & Bugs

1. **`lib/clob_client.py` signature_type bug:** Hardcodes `signature_type=0` but POLY_PROXY wallets need `signature_type=1`. All working scripts bypass this lib.
2. **`execute_trades.py` (v1) overpay bug:** Used `price=0.99` for all orders regardless of market price. Fixed in v2.
3. **Gamma API `slug_contains`:** Returns unrelated results. Use `tag` filter + client-side slug matching.
4. **`outcomePrices` format:** Can be `None`, a JSON string, or a list. Always handle all three cases:
   ```python
   prices_raw = market.get("outcomePrices")
   if prices_raw is None:
       continue  # Market not yet priced
   if isinstance(prices_raw, str):
       prices = json.loads(prices_raw)
   else:
       prices = prices_raw
   ```

---

## 3. Trading Strategy

### 3.1 Core Concept

Trade BTC 5-minute Up/Down markets by comparing **real-time BTC price movement** against **market-implied probability**. When the market misprices the direction (e.g., BTC is trending up but "Up" tokens are cheap), buy the underpriced outcome.

### 3.2 Strategy: Momentum + Mispricing

**Phase 1 — Market Selection (every 60s):**
1. Scan Gamma API for upcoming `btc-updown-5m-*` markets
2. Filter for markets starting in 1-4 minutes (enough time to analyze + trade)
3. Skip markets with `outcomePrices: None` (no liquidity yet)
4. Skip markets with liquidity < $500 (too thin to trade)

**Phase 2 — Signal Generation:**
1. Get current BTC price from Binance REST API (`GET /api/v3/ticker/price?symbol=BTCUSDT`)
2. Get BTC price from 5 minutes ago (or use kline data for recent trend)
3. Calculate short-term momentum: `momentum = (price_now - price_5m_ago) / price_5m_ago`
4. Get market's current Up/Down prices from orderbook
5. Compare: if momentum is positive but Up price < 0.55, there's a potential edge

**Phase 3 — Entry Decision:**
```
IF momentum > +0.05% AND up_price < 0.55:
    BUY "Up" tokens (market underpricing upward momentum)
ELIF momentum < -0.05% AND down_price < 0.55:
    BUY "Down" tokens (market underpricing downward momentum)
ELSE:
    SKIP (no clear edge)
```

**Phase 4 — Execution:**
- Place GTC limit order at `market_price + $0.02` (or best ask, whichever is lower)
- Maximum $1 per trade
- Hold to resolution (5-minute markets resolve too fast for active management)

**Phase 5 — Resolution:**
- Market resolves automatically via Chainlink
- Winning tokens worth $1.00, losing tokens worth $0.00
- Profit = $1.00 - entry_price (if correct), Loss = entry_price (if wrong)

### 3.3 Why Hold-to-Resolution

For 5-minute markets, active position management is impractical because:
- Bid-ask spreads are 2-5 cents (eating into any mid-market gains)
- Market window is only 5 minutes — by the time you detect a signal and trade, 1-2 minutes are already gone
- Selling before resolution incurs spread costs twice (buy spread + sell spread)
- Better to accept binary outcome: win big or lose entry price

### 3.4 Edge Calculation

```
Expected Value = P(correct) * (1.00 - entry_price) - P(wrong) * entry_price

Example: Buy "Up" at $0.45 when we estimate 55% chance of Up:
EV = 0.55 * (1.00 - 0.45) - 0.45 * 0.45
EV = 0.55 * 0.55 - 0.45 * 0.45
EV = 0.3025 - 0.2025 = $0.10 per dollar risked (22% edge)

Break-even accuracy needed at price P: accuracy > P
At $0.45 entry: need > 45% accuracy to be profitable
At $0.55 entry: need > 55% accuracy (tighter edge)
```

### 3.5 Price Feed: Binance REST API

```python
# Current BTC price
import httpx
resp = httpx.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
btc_price = float(resp.json()["price"])

# Recent klines (1-minute candles, last 10)
resp = httpx.get("https://api.binance.com/api/v3/klines", params={
    "symbol": "BTCUSDT",
    "interval": "1m",
    "limit": 10,
})
klines = resp.json()  # Each: [open_time, open, high, low, close, volume, ...]
```

**Important:** The market resolves based on **Chainlink BTC/USD**, not Binance. There may be small discrepancies. Binance is used as a proxy for momentum detection, not exact price matching.

### 3.6 Alternative Strategy: Orderbook Imbalance

Instead of (or in addition to) price momentum, monitor the Polymarket orderbook itself:
```python
book = client.get_order_book(up_token_id)
bid_depth = sum(float(b.size) * float(b.price) for b in book.bids)
ask_depth = sum(float(a.size) * float(a.price) for a in book.asks)
imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
# imbalance > 0.3 suggests buying pressure on "Up"
```

### 3.7 Capital Management

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Max per trade** | $1.00 | ~10% of estimated $10 capital |
| **Max daily loss** | $3.00 | Stop trading after 3 consecutive losses |
| **Max open positions** | 3 | Limit exposure across concurrent markets |
| **Reserve minimum** | $2.00 | Always keep $2 USDC.e for gas/recovery |
| **Min entry edge** | 10% | Only trade when estimated edge > 10% |

---

## 4. Implementation Plan

### 4.1 New Files to Create

```
polyclaw/
|-- lib/
|   |-- price_feed.py         # NEW: Binance BTC price + momentum calculation
|   |-- btc_market_scanner.py  # NEW: Gamma API scanner for btc-updown-5m markets
|   |-- signal_engine.py       # NEW: Entry signal generation (momentum + mispricing)
|-- scripts/
|   |-- btc_bot.py             # NEW: Main bot loop (scan -> signal -> trade -> wait)
|-- data/
    |-- btc_trades.json        # NEW: BTC bot trade history
    |-- btc_bot.log            # NEW: Bot execution log
```

### 4.2 Build Order (Step by Step)

#### Step 1: `lib/price_feed.py`
Simple module to get BTC price and calculate momentum.

```python
import httpx
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class PriceSnapshot:
    price: float
    timestamp: datetime
    momentum_5m: float  # % change over last 5 minutes
    trend: str  # "up", "down", "flat"

def get_btc_price() -> float:
    """Get current BTC/USDT price from Binance."""
    resp = httpx.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "BTCUSDT"},
        timeout=10,
    )
    return float(resp.json()["price"])

def get_btc_momentum() -> PriceSnapshot:
    """Get BTC price with 5-minute momentum from klines."""
    resp = httpx.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": 6},
        timeout=10,
    )
    klines = resp.json()
    price_now = float(klines[-1][4])   # latest close
    price_5m = float(klines[0][1])     # open of 5-min-ago candle
    momentum = (price_now - price_5m) / price_5m * 100
    trend = "up" if momentum > 0.05 else "down" if momentum < -0.05 else "flat"
    return PriceSnapshot(
        price=price_now,
        timestamp=datetime.now(timezone.utc),
        momentum_5m=momentum,
        trend=trend,
    )
```

#### Step 2: `lib/btc_market_scanner.py`
Scan Gamma API for upcoming BTC 5-minute markets.

```python
import httpx
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

@dataclass
class BtcMarket:
    event_id: str
    market_id: str
    slug: str
    question: str
    condition_id: str
    up_token_id: str      # clobTokenIds[0]
    down_token_id: str    # clobTokenIds[1]
    up_price: Optional[float]
    down_price: Optional[float]
    volume: float
    liquidity: float
    start_time: datetime  # Window start (from slug timestamp)
    end_time: datetime    # Window end (start + 300s)
    minutes_to_start: float
    minutes_to_end: float

def scan_btc_5m_markets(min_minutes=1.0, max_minutes=10.0) -> list:
    """Find BTC 5m markets starting within the given time window."""
    now = datetime.now(timezone.utc)
    resp = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={
            "limit": 100,
            "active": "true",
            "closed": "false",
            "order": "endDate",
            "ascending": "true",
            "tag": "crypto",
        },
        timeout=30,
    )
    events = resp.json()
    markets = []
    for event in events:
        slug = event.get("slug", "")
        if not slug.startswith("btc-updown-5m-"):
            continue
        start_ts = int(slug.split("-")[-1])
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(start_ts + 300, tz=timezone.utc)
        mins_to_start = (start_dt - now).total_seconds() / 60
        mins_to_end = (end_dt - now).total_seconds() / 60
        if mins_to_end < 0:
            continue  # Already ended
        event_markets = event.get("markets", [])
        if not event_markets:
            continue
        mkt = event_markets[0]  # Each 5m event has exactly 1 market
        # Parse prices
        prices_raw = mkt.get("outcomePrices")
        up_price = down_price = None
        if prices_raw:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            up_price = float(prices[0])
            down_price = float(prices[1])
        # Parse token IDs
        token_ids_raw = mkt.get("clobTokenIds")
        if not token_ids_raw:
            continue
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        markets.append(BtcMarket(
            event_id=str(event.get("id", "")),
            market_id=str(mkt.get("id", "")),
            slug=slug, question=mkt.get("question", ""),
            condition_id=mkt.get("conditionId", ""),
            up_token_id=token_ids[0], down_token_id=token_ids[1],
            up_price=up_price, down_price=down_price,
            volume=float(mkt.get("volume", 0) or 0),
            liquidity=float(mkt.get("liquidity", 0) or 0),
            start_time=start_dt, end_time=end_dt,
            minutes_to_start=mins_to_start, minutes_to_end=mins_to_end,
        ))
    markets.sort(key=lambda m: m.start_time)
    return [m for m in markets if min_minutes <= m.minutes_to_start <= max_minutes]
```

#### Step 3: `lib/signal_engine.py`
Combine price momentum with market pricing to generate trade signals.

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class TradeSignal:
    market_slug: str
    direction: str       # "up" or "down"
    token_id: str        # The token to buy
    entry_price: float   # Expected entry price
    edge: float          # Estimated edge (0.0 to 1.0)
    confidence: float    # Signal confidence (0.0 to 1.0)
    reason: str          # Human-readable explanation

def generate_signal(
    momentum_pct: float,
    trend: str,
    up_price: Optional[float],
    down_price: Optional[float],
    up_token_id: str,
    down_token_id: str,
    market_slug: str,
    min_edge: float = 0.10,
) -> Optional[TradeSignal]:
    """
    Generate a trade signal based on momentum vs market pricing.
    Returns None if no edge detected.
    """
    if up_price is None or down_price is None:
        return None

    # Estimate true probability from momentum
    # Base: 50/50. Adjust by momentum strength.
    # Momentum of +0.1% -> ~55% chance of Up
    # Momentum of +0.3% -> ~65% chance of Up
    # Cap at 75% (never be too confident on 5-min windows)
    momentum_factor = min(abs(momentum_pct) * 50, 25)  # max 25% adjustment

    if trend == "up":
        est_up_prob = 0.50 + momentum_factor / 100
        est_down_prob = 1.0 - est_up_prob
    elif trend == "down":
        est_down_prob = 0.50 + momentum_factor / 100
        est_up_prob = 1.0 - est_down_prob
    else:
        return None  # No clear trend, skip

    # Calculate edge for each direction
    up_edge = est_up_prob - up_price    # Positive = underpriced
    down_edge = est_down_prob - down_price

    # Pick the better edge
    if up_edge > down_edge and up_edge >= min_edge:
        reason = f"Momentum {momentum_pct:+.3f}%" + f" -> est {est_up_prob:.0%} Up vs market {up_price:.0%}"
        return TradeSignal(
            market_slug=market_slug, direction="up",
            token_id=up_token_id, entry_price=up_price,
            edge=up_edge, confidence=est_up_prob, reason=reason,
        )
    elif down_edge >= min_edge:
        reason = f"Momentum {momentum_pct:+.3f}%" + f" -> est {est_down_prob:.0%} Down vs market {down_price:.0%}"
        return TradeSignal(
            market_slug=market_slug, direction="down",
            token_id=down_token_id, entry_price=down_price,
            edge=down_edge, confidence=est_down_prob, reason=reason,
        )

    return None  # No sufficient edge
```

#### Step 4: `scripts/btc_bot.py`
Main bot loop that ties everything together.

**Architecture:**
```
LOOP (every 30-60 seconds):
  1. scan_btc_5m_markets() -> list of upcoming markets
  2. get_btc_momentum() -> current price trend
  3. For each market starting in 1-4 minutes:
     a. generate_signal(momentum, market_prices)
     b. If signal has edge >= 10%:
        - Check balance >= $3 ($1 trade + $2 reserve)
        - Check open positions < 3
        - Get orderbook for the token
        - Place GTC limit order at market_price + $0.02
        - Log trade to btc_trades.json
  4. Sleep 30-60 seconds
  5. Repeat
```

**Key implementation notes:**
- Use `ClobClient` directly (NOT `ClobClientWrapper` from lib — it has the signature_type bug)
- Always `signature_type=1` and `funder="0x146c795a032a01b8f9e872c7680a157eb17e31ee"`
- Rate limit: 1.5s between CLOB API calls
- Log everything: market slug, signal details, order ID, fill status
- Handle Cloudflare blocks with retry + exponential backoff
- Graceful shutdown on SIGINT/SIGTERM

**Skeleton:**
```python
import sys, os, time, json, signal, logging
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from lib.price_feed import get_btc_momentum
from lib.btc_market_scanner import scan_btc_5m_markets
from lib.signal_engine import generate_signal

load_dotenv()

# Config
PRIVATE_KEY = os.environ["POLYCLAW_PRIVATE_KEY"]
FUNDER = "0x146c795a032a01b8f9e872c7680a157eb17e31ee"
MAX_TRADE_USD = 1.00
MIN_BALANCE_USD = 3.00
MAX_OPEN_POSITIONS = 3
SCAN_INTERVAL = 45  # seconds
MIN_EDGE = 0.10

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("data/btc_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("btc_bot")

# CLOB client
client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY, chain_id=137,
    signature_type=1, funder=FUNDER,
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

def place_trade(token_id: str, price: float, size_usd: float) -> dict:
    """Place a GTC buy order. Returns order result dict."""
    book = client.get_order_book(token_id)
    asks = sorted(book.asks, key=lambda a: float(a.price)) if book.asks else []
    if asks:
        best_ask = float(asks[0].price)
        exec_price = round(min(price + 0.02, best_ask, 0.95), 2)
    else:
        exec_price = round(min(price + 0.02, 0.95), 2)
    exec_price = max(exec_price, 0.02)
    size = round(size_usd / exec_price, 0)
    while size * exec_price < 1.01:
        size += 1
    order_args = OrderArgs(price=exec_price, size=size, side=BUY, token_id=token_id)
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    return result

def main_loop():
    log.info("BTC 5m Bot starting...")
    traded_slugs = set()  # Avoid double-trading same market
    while True:
        try:
            # 1. Scan for upcoming markets
            markets = scan_btc_5m_markets(min_minutes=1.0, max_minutes=4.0)
            log.info(f"Found {len(markets)} upcoming BTC 5m markets")

            # 2. Get BTC momentum
            snapshot = get_btc_momentum()
            log.info(f"BTC ${snapshot.price:.0f} momentum={snapshot.momentum_5m:+.4f}% trend={snapshot.trend}")

            # 3. Check each market for signals
            for mkt in markets:
                if mkt.slug in traded_slugs:
                    continue
                sig = generate_signal(
                    momentum_pct=snapshot.momentum_5m,
                    trend=snapshot.trend,
                    up_price=mkt.up_price,
                    down_price=mkt.down_price,
                    up_token_id=mkt.up_token_id,
                    down_token_id=mkt.down_token_id,
                    market_slug=mkt.slug,
                    min_edge=MIN_EDGE,
                )
                if sig is None:
                    log.info(f"  {mkt.slug}: no signal (Up={mkt.up_price}, Down={mkt.down_price})")
                    continue
                log.info(f"  SIGNAL: {sig.direction} on {mkt.slug} edge={sig.edge:.1%} - {sig.reason}")

                # 4. Execute trade
                try:
                    result = place_trade(sig.token_id, sig.entry_price, MAX_TRADE_USD)
                    order_id = result.get("orderID", result.get("id", "unknown"))
                    log.info(f"  ORDER PLACED: {order_id}")
                    traded_slugs.add(mkt.slug)
                    time.sleep(1.5)  # Rate limit
                except Exception as e:
                    log.error(f"  Trade failed: {e}")

        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main_loop()
```

---

## 5. Component Specifications

### 5.1 Dependencies

All already installed in polyclaw venv:
```
py-clob-client    # Polymarket CLOB API client
httpx              # HTTP client (async + sync)
web3               # Ethereum/Polygon interaction
eth-account        # Wallet management
python-dotenv      # .env file loading
```

No new dependencies needed.

### 5.2 Import Map (what to import from where)

```python
# CLOB trading (use directly, NOT via lib/clob_client.py)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

# Existing polyclaw libs (safe to reuse)
from lib.wallet_manager import WalletManager
from lib.position_storage import PositionStorage, PositionEntry
from lib.contracts import CONTRACTS
from lib.gamma_client import GammaClient, Market

# New components (to be created)
from lib.price_feed import get_btc_price, get_btc_momentum, PriceSnapshot
from lib.btc_market_scanner import scan_btc_5m_markets, BtcMarket
from lib.signal_engine import generate_signal, TradeSignal
```

### 5.3 Data Flow

```
Binance API ──> price_feed.py ──> PriceSnapshot
                                       |
Gamma API ──> btc_market_scanner.py ──> BtcMarket[]
                                       |
              signal_engine.py <────────+
                    |
                    v
              TradeSignal (or None)
                    |
                    v
              btc_bot.py ──> ClobClient.post_order()
                    |
                    v
              btc_trades.json (log)
```

### 5.4 Gamma API Response Structure (for BTC 5m events)

Each event from `GET /events` with `tag=crypto` has this structure:
```json
{
  "id": 262662,
  "slug": "btc-updown-5m-1773390000",
  "title": "Bitcoin Up or Down - March 13, 4:20AM-4:25AM ET",
  "endDate": "2026-03-13T08:25:00Z",
  "markets": [
    {
      "id": 1565761,
      "question": "Bitcoin Up or Down - March 13, 4:20AM-4:25AM ET",
      "conditionId": "0xc2b6...",
      "slug": "bitcoin-up-or-down-march-13-420am-425am-et",
      "outcomes": "[\"Up\",\"Down\"]",
      "outcomePrices": "[\"0.425\",\"0.575\"]",
      "clobTokenIds": "[\"58633...008\",\"86920...047\"]",
      "volume": "16865.42",
      "liquidity": "12027.15",
      "active": true,
      "closed": false,
      "negRisk": false
    }
  ]
}
```

**Parsing notes:**
- `outcomes`, `outcomePrices`, `clobTokenIds` are JSON-encoded strings (double-encoded)
- Always `json.loads()` them before use
- `outcomePrices` can be `None` for markets with no trading activity yet
- `volume` and `liquidity` are strings, cast to float
- Event `slug` contains the start timestamp; market `slug` is a human-readable version

### 5.5 CLOB Orderbook Structure

```python
book = client.get_order_book(token_id)
# book.asks = list of OrderBookEntry(price="0.55", size="100")
# book.bids = list of OrderBookEntry(price="0.45", size="50")
# Prices and sizes are strings, cast to float
# asks sorted ascending by price (cheapest first)
# bids sorted descending by price (highest first)
```

### 5.6 Order Result Structure

```python
result = client.post_order(signed_order, OrderType.GTC)
# Success: {"orderID": "0xabc...", "status": "live", ...}
# Also seen: {"id": "0xabc...", ...}
# Always check: result.get("orderID", result.get("id", "unknown"))
#
# Check fill status later:
# order_info = client.get_order(order_id)
# order_info["status"] == "MATCHED" means fully filled
```

---

## 6. Risk Management

### 6.1 Position Limits

| Rule | Value | Enforcement |
|------|-------|-------------|
| Max trade size | $1.00 | Check before order placement |
| Max open positions | 3 | Count active trades in btc_trades.json |
| Min wallet balance | $2.00 USDC.e | Check via WalletManager before trading |
| Max daily loss | $3.00 | Track cumulative losses, stop bot if exceeded |
| Max consecutive losses | 5 | Pause bot for 30 minutes after 5 losses |

### 6.2 Market Quality Filters

| Filter | Threshold | Reason |
|--------|-----------|--------|
| Minimum liquidity | $500 | Avoid illiquid markets with wide spreads |
| Price range | $0.10 - $0.90 | Avoid extreme prices (low ROI or high risk) |
| Time to start | 1-4 minutes | Too early = stale signal; too late = can't fill |
| Minimum edge | 10% | Only trade when estimated probability edge > 10% |

### 6.3 Error Handling

| Error | Action |
|-------|--------|
| Binance API timeout | Skip this cycle, retry next loop |
| Gamma API timeout | Skip this cycle, retry next loop |
| CLOB order rejected | Log error, skip market, continue |
| Cloudflare block (403) | Wait 30s, retry with proxy if available |
| Insufficient balance | Stop bot, notify via log |
| Network error | Exponential backoff (5s, 10s, 20s, 40s) |

### 6.4 No Stop-Loss / Take-Profit

For 5-minute markets, **hold to resolution** is the optimal strategy:
- Markets resolve in 5 minutes — too fast for active management
- Selling early incurs bid-ask spread costs (2-5 cents each way)
- Binary outcome: win $1.00 or lose entry price
- No stop-loss needed because max loss = entry price ($1.00)

---

## 7. Quick Start

### 7.1 Prerequisites

```bash
cd /a0/usr/projects/trading/polyclaw
source .venv/bin/activate  # or: source /a0/usr/workdir/polyclaw/venv/bin/activate
# Verify .env has POLYCLAW_PRIVATE_KEY set
cat .env | grep POLYCLAW
```

### 7.2 Implementation Steps

1. **Create `lib/price_feed.py`** — Copy code from Section 4.2 Step 1
2. **Create `lib/btc_market_scanner.py`** — Copy code from Section 4.2 Step 2
3. **Create `lib/signal_engine.py`** — Copy code from Section 4.2 Step 3
4. **Create `scripts/btc_bot.py`** — Copy skeleton from Section 4.2 Step 4
5. **Test each component individually:**
   ```bash
   # Test price feed
   python -c "from lib.price_feed import get_btc_momentum; print(get_btc_momentum())"
   
   # Test market scanner
   python -c "from lib.btc_market_scanner import scan_btc_5m_markets; print(scan_btc_5m_markets(0, 30))"
   
   # Test signal engine (with mock data)
   python -c "from lib.signal_engine import generate_signal; print(generate_signal(0.15, 'up', 0.45, 0.55, 'tok1', 'tok2', 'test'))"
   ```
6. **Dry run** — Run btc_bot.py with order placement commented out, verify signals
7. **Live run** — Enable order placement with $1 max trades

### 7.3 Running the Bot

```bash
cd /a0/usr/projects/trading/polyclaw
source .venv/bin/activate
python scripts/btc_bot.py
# Logs to: data/btc_bot.log and stdout
# Ctrl+C to stop
```

### 7.4 Monitoring

```bash
# Watch live logs
tail -f data/btc_bot.log

# Check trade history
cat data/btc_trades.json | python -m json.tool

# Check wallet balance
python scripts/wallet.py
```

---

## Appendix A: Corrected Information

**Previous incorrect assumption:** "Polymarket does NOT offer 5-minute BTC markets."

**Corrected fact (verified 2026-03-13):** Polymarket DOES offer 5-minute BTC Up/Down markets.
They are generated every 5 minutes with slug pattern `btc-updown-5m-{timestamp}`.
Similar markets exist for ETH and SOL. These markets have active trading with
$10k-$20k volume and $10k+ liquidity per market.

---

## Appendix B: Reference Implementation

**Repository:** `aulekator/Polymarket-BTC-15-Minute-Trading-Bot`

This is a reference implementation for 15-minute BTC markets (which may or may not exist).
Its 7-phase architecture was studied but our implementation differs because:
1. We target 5-minute markets (not 15-minute)
2. We use hold-to-resolution (not active position management)
3. We use Binance REST API (not WebSocket — simpler, sufficient for 45s scan interval)
4. We reuse existing polyclaw infrastructure (not standalone)

---

*Document generated: 2026-03-13. All API responses and code patterns verified against live systems.*
