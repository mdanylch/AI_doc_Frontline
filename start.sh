#!/bin/sh
# App Runner build command: `sh start.sh`
# Install deps only — must exit during image build (do not start the server here).
#
# App Runner start command: `sh run.sh` (see run.sh).
set -e
python3 -m pip install --upgrade pip setuptools wheel 2>/dev/null || true
pip3 install --no-cache-dir -r requirements.txt
