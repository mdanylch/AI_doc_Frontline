#!/bin/sh
# App Runner start command: `sh run.sh`
# Dependencies are installed under /app/vendor during build (`sh start.sh`).
set -e
export PORT="${PORT:-8080}"
export PYTHONPATH="/app/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec python3 mcp_server.py
