#!/bin/bash
# PolyBMiCoB Docker entrypoint
# Starts the dashboard in background, then runs the bot in foreground.

set -e

echo "Starting dashboard on port ${DASHBOARD_PORT:-8005}..."
python /app/web/dashboard.py &
DASH_PID=$!

# Give dashboard a moment to start
sleep 1

# Determine bot mode
# v2 = event-driven architecture (EventBus + MarketClock + Polymarket WS)
if [ "${BOT_MODE}" = "live" ]; then
    echo "Starting bot v2 (event-driven) in LIVE mode..."
    python /app/scripts/btc_bot_v2.py
else
    echo "Starting bot v2 (event-driven) in DRY-RUN mode..."
    python /app/scripts/btc_bot_v2.py --dry-run
fi

# If bot exits, also stop dashboard
kill $DASH_PID 2>/dev/null || true
