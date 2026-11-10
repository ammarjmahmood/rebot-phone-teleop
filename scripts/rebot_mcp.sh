#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python -m rebot_teleop.mcp_server "$@"
