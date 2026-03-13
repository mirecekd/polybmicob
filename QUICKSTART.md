# Quick Start

## TL;DR

```bash
cp .env.example .env           # fill in your private key + proxy wallet
docker compose up --build      # bot + dashboard, done
open http://localhost:8005     # watch it work
```

**What happens:**

1. Docker builds the image, installs dependencies
2. Dashboard starts at `http://localhost:8005`
3. Bot starts (dry-run or live based on `BOT_MODE` in `.env`)
4. Bot scans markets every 30s, analyzes momentum + orderbook + sentiment
5. When edge > 10% is found, places order for max $1
6. Everything visible on dashboard + in `data/btc_bot.log`

> **Warning:** If `BOT_MODE=live` in `.env`, the bot trades with real money immediately!
> Default is `BOT_MODE=dry-run` (safe, no real orders).

---

Get the bot running in 2 minutes. Choose **Docker** (recommended) or **local Python**.

---

## Option A: Docker (Recommended)

No Python setup needed. Just Docker.

```bash
cd ~/DEVEL/polybmicob

# 1. Configure (one-time)
cp .env.example .env
# Edit .env: set POLYBMICOB_PRIVATE_KEY and POLYMARKET_PROXY_WALLET

# 2. Run in dry-run mode (no real trades)
docker compose up --build

# 3. Open dashboard in browser
open http://localhost:8005
```

That's it. The bot scans markets every 45 seconds. The dashboard auto-refreshes every 30 seconds.

### Switch to Live Trading

```bash
# Live mode - places real orders with real money!
BOT_MODE=live docker compose up
```

### Run in Background

```bash
docker compose up -d          # dry-run, background
docker compose logs -f        # watch logs
docker compose down           # stop
```

---

## Option B: Local Python

### Prerequisites

- Python 3.12+
- `virtualenvwrapper` installed
- `.env` file configured (see above)
- Funded Polymarket wallet (min $3 USDC.e on Polygon)

### Setup

```bash
# 1. Create virtual environment (one-time)
mkvirtualenv polybmicob -p python3

# 2. Install dependencies
cd ~/DEVEL/polybmicob
workon polybmicob
pip install -r requirements.txt
```

### Run Bot (Dry Run)

```bash
cd ~/DEVEL/polybmicob
workon polybmicob
python scripts/btc_bot.py --dry-run
```

### Run Bot (Live)

```bash
cd ~/DEVEL/polybmicob
workon polybmicob
python scripts/btc_bot.py
```

**Warning:** Live mode places real orders with real money. Max $1.00 per trade.

### Run Dashboard (Standalone)

```bash
# In a separate terminal:
cd ~/DEVEL/polybmicob
workon polybmicob
python web/dashboard.py
# Open http://localhost:8005
```

---

## Expected Dry-Run Output

```
============================================================
PolyBMiCoB - BTC Micro-Cycle Options Bot
Mode: DRY RUN
Config: max_trade=$1.00, min_edge=10%, scan=1-6m, interval=45s
============================================================
Found 1 upcoming BTC 5m markets
BTC $72250  momentum=-0.0533%  trend=down
Fear & Greed Index: 15 (Extreme Fear)
  btc-updown-5m-1773403500: SKIP (edge 0.5% < 10% threshold)
    Would buy DOWN @ $0.49 -> win $1.02 / lose $1.00
    [momentum=+0.004% ob_imbalance=-0.03 F&G=15 -> est 50% Down vs market 50%]
--- Sleeping 45s ---
```

The bot shows for every market: what it would buy, at what price, potential win/loss, and why it decided to trade or skip.

---

## Dashboard

Open `http://localhost:8005` to see:

- **Stat cards** -- total trades, live/dry split, UP/DOWN count, avg edge, expected P&L
- **Trades table** -- last 50 trades with all details
- **Bot log** -- live tail of the last 40 log lines
- **Auto-refresh** every 30 seconds

Also available as JSON:

- `http://localhost:8005/api/trades` -- full trade history
- `http://localhost:8005/api/stats` -- computed statistics

---

## Monitoring

```bash
# Watch live logs
tail -f ~/DEVEL/polybmicob/data/btc_bot.log

# View trade history
cat ~/DEVEL/polybmicob/data/btc_trades.json | python -m json.tool

# Docker logs
docker compose logs -f
```

---

## Test Individual Components

```bash
cd ~/DEVEL/polybmicob
workon polybmicob

# Test price feed
python -c "
from lib.price_feed import get_btc_momentum, get_fear_greed_index
m = get_btc_momentum()
print(f'BTC: \${m.price:.0f}, momentum: {m.momentum_5m:+.4f}%, trend: {m.trend}')
fg = get_fear_greed_index()
print(f'Fear&Greed: {fg.value} ({fg.classification})' if fg else 'N/A')
"

# Test market scanner
python -c "
from lib.btc_market_scanner import scan_btc_5m_markets
markets = scan_btc_5m_markets(0, 15)
print(f'Found {len(markets)} markets')
for m in markets:
    print(f'  {m.slug}: Up={m.up_price}, Down={m.down_price}, liq=\${m.liquidity:.0f}')
"

# Test signal engine (with mock data)
python -c "
from lib.signal_engine import generate_signal
sig = generate_signal(0.15, 'up', 0.45, 0.55, 'tok1', 'tok2', 'test', fear_greed_value=50)
print(f'{sig.direction} edge={sig.edge:.1%} - {sig.reason}' if sig else 'no signal')
"
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `POLYBMICOB_PRIVATE_KEY not set` | Check that `.env` exists and contains the key |
| `Found 0 upcoming BTC 5m markets` | Markets may not be pre-generated yet. Wait a few minutes and retry |
| `CLOB client init failed` | Network issue or Cloudflare block. Bot will retry next cycle |
| `no signal` on every market | Normal when prices are near 50/50 and momentum is flat. The bot only trades with 10%+ edge |
| `HTTP/2 400 Bad Request` on auth | Normal first-time behavior. The client falls back to `derive-api-key` automatically |
| Dashboard shows "No trades yet" | Run the bot first. Dashboard reads `data/btc_trades.json` |
| Docker port conflict on 8005 | Change `DASHBOARD_PORT=3000` in `.env` and update `docker-compose.yml` ports |
