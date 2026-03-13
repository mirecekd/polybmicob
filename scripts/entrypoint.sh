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
if [ "${BOT_MODE}" = "live" ]; then
    echo "Starting bot in LIVE mode..."
    python /app/scripts/btc_bot.py
else
    echo "Starting bot in DRY-RUN mode..."
    python /app/scripts/btc_bot.py --dry-run
fi

# If bot exits, also stop dashboard
kill $DASH_PID 2>/dev/null || true
