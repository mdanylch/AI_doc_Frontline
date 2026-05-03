#!/bin/sh
# App Runner start command: `sh run.sh`
# Dependencies are installed under /app/vendor during build (`sh start.sh`).
#
# PYTHONUNBUFFERED + python -u: stdout/stderr flush immediately so CloudWatch
# Application logs show lines without waiting for buffer fills (Python default).
set -e
export PORT="${PORT:-8080}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONPATH="/app/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -u mcp_server.py
