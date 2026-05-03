#!/bin/sh
# AWS App Runner / Fusion Python build: this script is executed during `docker build`
# (`RUN sh start.sh`). It must finish quickly — install deps only. Do not start the MCP
# server here or the build will hang or fail.
#
# At runtime, use apprunner.yaml `run.command` or the console start command:
#   python3 mcp_server.py
set -e
python3 -m pip install --upgrade pip setuptools wheel 2>/dev/null || true
pip3 install --no-cache-dir -r requirements.txt
