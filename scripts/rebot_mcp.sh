#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
export REBOT_URL="${REBOT_URL:-http://127.0.0.1:8081}"
exec .venv/bin/python -m rebot_teleop.mcp_server "$@"
