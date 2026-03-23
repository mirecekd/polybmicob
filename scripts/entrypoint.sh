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
# v3 = clean arb MM (event-driven, no rescue/last-resort, strict pair_cost<$1.00)
if [ "${BOT_MODE}" = "live" ]; then
    echo "Starting bot v3 (clean arb MM) in LIVE mode..."
    python /app/scripts/btc_bot_v3.py
else
    echo "Starting bot v3 (clean arb MM) in DRY-RUN mode..."
    python /app/scripts/btc_bot_v3.py --dry-run
fi

# If bot exits, also stop dashboard
kill $DASH_PID 2>/dev/null || true
