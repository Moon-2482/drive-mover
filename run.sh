#!/bin/bash
set -e
cd "$(dirname "$0")"
export PORT="${PORT:-8766}"
export HOST="${HOST:-127.0.0.1}"
exec python3 server.py
