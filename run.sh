#!/bin/sh
# App Runner start command: `sh run.sh`
# Dependencies are installed during build via `sh start.sh`.
set -e
export PORT="${PORT:-8080}"
exec python3 mcp_server.py
