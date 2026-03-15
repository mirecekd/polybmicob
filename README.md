# PolyBMiCoB - Polymarket BTC Micro-Cycle Options Bot

Automated trading bot for **BTC 5-minute Up/Down prediction markets** on [Polymarket](https://polymarket.com). Analyzes real-time Bitcoin price momentum, orderbook depth, and market sentiment to find mispriced outcomes and place trades with a positive expected value.

---

## How It Works

Every 20 seconds, the bot:

1. **Scans** Gamma API for upcoming BTC 5-minute markets (starting in 18s-6min)
2. **Fetches** BTC price and 5-minute momentum from Binance
3. **Analyzes** Polymarket CLOB orderbooks for bid/ask imbalance
4. **Fuses** three signals (momentum, orderbook, sentiment) to estimate true probability
5. **Trades** via FOK (Fill or Kill) orders with retry + backoff - instant fill or cancel, no ghost orders
6. **Holds to resolution** - 5-minute markets resolve too fast for active management
7. **In-play mode**: Also analyzes markets already running (30-210s after start) by comparing real BTC movement with token prices

### The Markets

Polymarket runs binary prediction markets every 5 minutes:

> "Will Bitcoin's price be higher or lower in 5 minutes?"

- Slug pattern: `btc-updown-5m-{UNIX_TIMESTAMP}`
- Two outcomes: **Up** (price >= start) or **Down** (price < start)
- Resolution: [Chainlink BTC/USD data stream](https://data.chain.link/streams/btc-usd)
- Typical liquidity: $10,000-$20,000 per market

### The Edge

If BTC is trending up (+0.15% over 5 min) but the "Up" token is priced at $0.45 (implying 45% probability), we estimate the true probability is closer to 57%. That's a 12% edge:

```
Expected Value = 0.57 * $0.55 - 0.43 * $0.45 = +$0.12 per dollar
```

---

## Architecture

```
Binance REST API ---- price_feed.py ---- PriceSnapshot (price, momentum, trend)
                                              |
Alternative.me   ---- price_feed.py ---- FearGreedData (sentiment 0-100)
                                              |
Gamma API        ---- btc_market_scanner.py - BtcMarket[] (upcoming markets)
                                              |
Polymarket CLOB  ---- btc_bot.py ----------- OrderbookSignal (bid/ask imbalance)
                                              |
                      signal_engine.py <------+---- Weighted Fusion (3 signals)
                           |
                           v
                      TradeSignal (or None)
                           |
                           v
                      btc_bot.py ---- ClobClient.post_order() ---- Polymarket CLOB
                           |
                           v
                      data/btc_trades.json + data/btc_bot.log
```

### Signal Fusion Weights

| Signal | Weight | Source | What It Measures |
|--------|--------|--------|------------------|
| **Orderbook Imbalance** | 45% | Polymarket CLOB | Bid/ask depth ratio on Up vs Down tokens |
| **Price Momentum** | 40% | Binance REST API | BTC price change over last 5 minutes |
| **Sentiment** | 15% | Alternative.me | Fear & Greed Index dampening/amplifying factor |

These weights are inspired by the [Polymarket-BTC-15-Minute-Trading-Bot](https://github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot) reference implementation, which found orderbook imbalance to be the strongest predictor.

---

## Project Structure

```
polybmicob/
|-- README.md                  # This file
|-- QUICKSTART.md              # Get running in 2 minutes
|-- SPEC.md                    # Original development specification (1000+ lines)
|-- requirements.txt           # Python dependencies
|-- lib/
|   |-- __init__.py
|   |-- price_feed.py          # Binance BTC price + momentum + Fear & Greed Index
|   |-- btc_market_scanner.py  # Gamma API scanner for btc-updown-5m-* markets
|   |-- signal_engine.py       # Weighted signal fusion engine (3 sources)
|   |-- claim_winnings.py      # Auto-claim resolved winnings via gasless relayer
|-- scripts/
|   |-- btc_bot.py             # Main bot loop (scan -> signal -> trade -> wait)
|   |-- claim_winnings.py      # Standalone claim script (manual or cron)
|-- web/
|   |-- dashboard.py           # Web dashboard (port 8005)
|-- data/
    |-- btc_trades.json        # Trade history (created at runtime)
    |-- btc_bot.log            # Bot execution log (created at runtime)
    |-- claim_winnings.log     # Claim log (created at runtime)
```

### Components

**`lib/price_feed.py`** - BTC price data from Binance REST API. Gets current price, 5-minute momentum from 1m klines, and trend classification (up/down/flat at +/-0.05% threshold). Also fetches Fear & Greed Index from alternative.me with 30-minute caching.

**`lib/btc_market_scanner.py`** - Discovers upcoming markets by generating expected slugs on the 300-second grid and querying Gamma API by exact slug. The generic `tag=crypto` filter doesn't return 5m markets reliably, so this approach is more robust.

**`lib/signal_engine.py`** - Weighted signal fusion combining momentum probability estimation, orderbook imbalance analysis, and sentiment dampening. Outputs a `TradeSignal` with direction (up/down), confidence, edge, and human-readable reasoning, or `None` if no sufficient edge exists.

**`scripts/btc_bot.py`** - Main bot loop with `--dry-run` support. Uses `ClobClient` directly with `signature_type=1` (POLY_PROXY), bypassing the known `signature_type=0` bug in polyclaw's `ClobClientWrapper`. Includes all risk management, trade logging, and graceful shutdown.

---

## External APIs

| API | Purpose | Auth | Cost |
|-----|---------|------|------|
| [Binance REST](https://api.binance.com) | BTC/USDT price + 1m klines | None | Free |
| [Alternative.me Fear & Greed](https://api.alternative.me/fng/) | Market sentiment index | None | Free |
| [Gamma API](https://gamma-api.polymarket.com) | Market discovery + metadata | None | Free |
| [Polymarket CLOB](https://clob.polymarket.com) | Orderbook data + order placement | API key (auto-derived) | Free |

No LLM, no search APIs, no paid services. Purely algorithmic.

---

## Risk Management

| Rule | Value | Purpose |
|------|-------|---------|
| Max trade size | $1.00 | Limit per-trade exposure |
| Min wallet balance | $3.00 | Keep $2 reserve + $1 for trade |
| Max daily loss | $3.00 | Stop bot after cumulative losses |
| Max consecutive losses | 5 | Pause 30 minutes after 5 losses |
| Min market liquidity | $500 | Avoid illiquid markets |
| Valid price range | $0.10 - $0.90 | Avoid extreme prices |
| Min edge threshold | 10% | Only trade with clear advantage |
| Time window | 1-6 minutes to start | Not too early (stale), not too late (can't fill) |

### Hold-to-Resolution Strategy

For 5-minute markets, there is no stop-loss or take-profit:

- Markets resolve in 5 minutes - too fast for active management
- Selling early incurs bid-ask spread costs twice
- Binary outcome: win $1.00 per share or lose entry price
- Maximum loss per trade = entry price ($1.00 max)

---

## Configuration

All configuration lives in `.env` (see `.env.example` for a template). The bot reads it on startup:

```bash
# Trading parameters
MAX_TRADE_USD=1.00            # Max $ per trade
MIN_BALANCE_USD=3.00          # Min wallet balance to trade
MAX_OPEN_POSITIONS=3          # Max concurrent trades
MIN_EDGE=0.10                 # 10% minimum edge
MAX_DAILY_LOSS_USD=3.00       # Daily loss limit
MAX_CONSECUTIVE_LOSSES=5      # Pause after this many losses

# Timing
SCAN_INTERVAL_SEC=45          # Seconds between scan cycles
SCAN_MIN_MINUTES=1.0          # Min minutes to market start
SCAN_MAX_MINUTES=6.0          # Max minutes to market start
RATE_LIMIT_SEC=1.5            # Seconds between CLOB API calls
```

### Wallet Setup

The bot reads wallet credentials from `.env` (see `.env.example`):

```
POLYBMICOB_PRIVATE_KEY=<your_polygon_private_key>
POLYMARKET_PROXY_WALLET=<your_proxy_wallet_address>
```

Wallet details:

- **Proxy wallet (funder):** Your Polymarket proxy wallet address (from Polymarket UI > Profile > Address)
- **Signature type:** 1 (POLY_PROXY for Magic Link accounts)
- **Chain:** Polygon (chain_id=137)

> **Funding your wallet:** You need USDC on the Polygon network in your Polymarket proxy wallet.
> The easiest way is to buy USDC via Revolut (or any exchange) and send it directly to your
> Polymarket proxy wallet address (`POLYMARKET_PROXY_WALLET` in `.env`). This is the address
> that holds your funds and that the bot trades with. Minimum recommended: $5 USDC.e ($3 reserve + $2 for trades).

---

## Trade Log Format

Each trade is appended to `data/btc_trades.json`:

```json
{
  "timestamp": "2026-03-13T12:00:00+00:00",
  "slug": "btc-updown-5m-1773403200",
  "direction": "up",
  "token_id": "58633653608407954532...",
  "entry_price": 0.45,
  "edge": 0.12,
  "confidence": 0.57,
  "reason": "momentum=+0.150% ob_imbalance=+0.30 F&G=50 -> est 57% Up vs market 45%",
  "order_id": "0xabc123...",
  "dry_run": false,
  "btc_price": 72250.0,
  "momentum": 0.15,
  "fear_greed": 50
}
```

---

## Edge Calculation

```
Expected Value = P(correct) * (1.00 - entry_price) - P(wrong) * entry_price

At $0.45 entry with 57% estimated probability:
  EV = 0.57 * 0.55 - 0.43 * 0.45 = 0.3135 - 0.1935 = +$0.12 per dollar (27% ROI)

Break-even accuracy at price P: need accuracy > P
  At $0.45: need > 45% accuracy
  At $0.55: need > 55% accuracy
```

---

## Relation to Polyclaw

PolyBMiCoB is a standalone project that **reuses polyclaw's wallet configuration** (`.env` file) but does not import any polyclaw library code. This avoids the known `signature_type=0` bug in polyclaw's `ClobClientWrapper` and keeps the codebase independent.

The bot uses `py-clob-client` directly with the correct `signature_type=1` configuration, matching the pattern proven in polyclaw's `autotrader.py` and `execute_trades_v2.py`.

---

## Docker

The entire bot + dashboard runs in a single Docker container. Just provide your `.env` and go.

### Quick Docker Start

```bash
# 1. Copy and fill in your config
cp .env.example .env
# Edit .env with your POLYBMICOB_PRIVATE_KEY and POLYMARKET_PROXY_WALLET

# 2. Run in dry-run mode (default, no real trades)
docker compose up --build

# 3. Open dashboard
open http://localhost:8005
```

### Docker Modes

| Command | Mode | Trades? | Dashboard? |
|---------|------|---------|------------|
| `docker compose up` | Dry-run | No (simulated) | Yes, port 8005 |
| `BOT_MODE=live docker compose up` | Live | Yes, real $$ | Yes, port 8005 |
| `docker compose up -d` | Dry-run, background | No | Yes, port 8005 |
| `BOT_MODE=live docker compose up -d` | Live, background | Yes | Yes, port 8005 |

### Switching Between Dry-Run and Live

The `BOT_MODE` environment variable controls the mode:

```bash
# Dry-run (default) - no real trades, just analysis
docker compose up

# Live trading - places real orders with real money!
BOT_MODE=live docker compose up

# Or set it in .env:
echo "BOT_MODE=live" >> .env
docker compose up
```

### Docker Operations

```bash
# Rebuild after code changes
docker compose up --build

# Run in background
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Stop and remove data
docker compose down -v
```

### Volume Mount

The `data/` directory is mounted as a volume, so trade history and logs persist across container restarts:

- `data/btc_trades.json` -- trade history (survives restarts)
- `data/btc_bot.log` -- bot logs

---

## Web Dashboard

A built-in web dashboard shows live trade statistics. It runs automatically alongside the bot (both in Docker and locally).

### What It Shows

- **Hero cards:** Real P&L, Win Rate, Streak, Total Trades
- **Stats row:** UP/DOWN win rates, Avg Confidence
- **Today's activity cards:** Cycles, Mom. Skips, Pre/In-Play Signals, Filled, Rejected, Resolved
- **SVG charts:** Cumulative P&L line chart + Trade dots (WIN/LOSS by entry price)
- **Recent Trades:** Last 50 trades with mode (PRE/IN-PLAY), direction, edge, result, P&L
- **In-Play Signal Analysis:** Every in-play signal with outcome (filled/low_liq/api_error/fok_killed), rejection reason badges
- **Hourly Breakdown:** Per-hour cycles, momentum skips, signals, filled/rejected orders
- **Bot Log:** Last 40 lines in real-time
- **ET time:** Polymarket-local Eastern Time displayed next to UTC
- **Auto-refresh:** Every 30 seconds

### Running the Dashboard

```bash
# Automatically with Docker
docker compose up
# Dashboard at http://localhost:8005

# Standalone (without Docker)
cd ~/DEVEL/polybmicob
workon polybmicob
python web/dashboard.py                 # default port 8005
python web/dashboard.py --port 3000     # custom port
```

### API Endpoints

The dashboard also exposes JSON APIs:

| Endpoint | Returns |
|----------|---------|
| `GET /` | HTML dashboard |
| `GET /api/trades` | Full trade history as JSON |
| `GET /api/stats` | Computed statistics as JSON |

### Dashboard Port

Change the port via environment variable:

```bash
# In .env
DASHBOARD_PORT=3000

# Or via docker-compose override
docker compose up -e DASHBOARD_PORT=3000
```

---

## Auto-Claim Winnings

The bot automatically claims resolved winning positions via Polymarket's **gasless Builder Relayer** - no MATIC needed for gas.

### How It Works

1. Every 10 bot cycles (~5 min), queries Polymarket Data API for redeemable positions
2. For each winning position, encodes `redeemPositions()` calldata
3. Submits via Builder Relayer as a PROXY wallet transaction (gasless)
4. USDC.e is returned to your proxy wallet

### Setup

Register at [builders.polymarket.com](https://builders.polymarket.com) (free, Unverified tier = 100 tx/day) and add credentials to `.env`:

```bash
POLY_BUILDER_API_KEY=your-api-key
POLY_BUILDER_SECRET=your-secret
POLY_BUILDER_PASSPHRASE=your-passphrase
```

### Standalone Claim Script

```bash
# Check what's claimable (no transactions)
python scripts/claim_winnings.py --check

# Dry-run (detect only, don't claim)
python scripts/claim_winnings.py

# Actually claim via gasless relayer
python scripts/claim_winnings.py --live
```

### Bot Integration

When Builder credentials are set, the bot auto-claims every 10 cycles. Configure the interval:

```bash
CLAIM_EVERY_N_CYCLES=10    # Check every 10 cycles (default)
```

---

## Backtesting

Standalone script to validate the strategy against historical BTC data from Binance.

```bash
python scripts/backtest.py                     # last 24 hours
python scripts/backtest.py --hours 72          # last 3 days
python scripts/backtest.py --hours 168         # last week
python scripts/backtest.py --edge 0.10         # higher edge threshold
```

**Important:** Backtesting only uses momentum signal (40% weight). It does NOT have access to historical Polymarket orderbook data (45% weight), so results are conservative compared to live trading. Live trading achieved 52.9% WR vs backtest ~46%.

### Output Example

```
BACKTEST RESULTS - Last 24h (288 candles)
  Signals generated:  121 (skipped 166, min_edge=5%)
  Win Rate:           46.3%
  Simulated P&L:      $-4.50
  UP bets:    28W / 33L  (46%)
  DOWN bets:  28W / 32L  (47%)
  Hour (UTC)  Signals  WinRate
  00:00          8       75%
  14:00          8       75%
  ...
```

---

## Resolution Tracking

The bot automatically tracks win/loss outcomes for every trade via CLOB API.

- Every 5 bot cycles (~2.5 min), checks unresolved trades against CLOB `/markets/{conditionId}`
- Matches `tokens[].winner` to determine if our bet won or lost
- Updates `data/btc_trades.json` with `resolved`, `won`, `pnl` fields
- Dashboard shows real P&L, win rate, streak, and UP/DOWN breakdown

---

## Deployment (GHCR)

Pre-built multi-arch images (amd64 + arm64) are published via GitHub Actions CI/CD to GHCR:

```bash
# Pull and run (e.g. on Raspberry Pi)
docker pull ghcr.io/mirecekd/polybmicob
docker run -d -p 8005:8005 \
  --name polybmicob \
  --restart always \
  --env-file /path/to/.env \
  -v /path/to/data:/app/data \
  ghcr.io/mirecekd/polybmicob
```

---

## Development Notes

- **Python 3.12+** required
- **Virtual environment:** `workon polybmicob`
- **Reference implementation:** Studied [Polymarket-BTC-15-Minute-Trading-Bot](https://github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot) for signal fusion architecture
