#!/bin/bash
# Watchdog wrapper for Guts — restarts main.py if it dies.
# Usage: ./run.sh  (run in background: nohup ./run.sh > watchdog.log 2>&1 &)

cd "$(dirname "$0")"

# Persona dashboard (localhost:8766) — start once, alongside the bot, so the link is always
# live after a restart. It's independent of main.py's crash/restart loop, so we don't relaunch
# it each iteration. Guard against a duplicate if one's already running.
if ! pgrep -f "persona_viewer.py" >/dev/null 2>&1; then
    echo "[$(date)] Starting persona dashboard on :8766..."
    nohup python3 persona_viewer.py --port 8766 > persona_viewer.log 2>&1 &
fi

while true; do
    echo "[$(date)] Starting Guts..."
    python3 main.py
    EXIT_CODE=$?
    echo "[$(date)] Guts exited with code $EXIT_CODE. Restarting in 5s..."
    sleep 5
done
