#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
if [ -f "$HOME/.rebot-teleop/agent.env" ]; then
  set -a
  source "$HOME/.rebot-teleop/agent.env"
  set +a
fi
exec .venv/bin/python -m rebot_teleop.mcp_server "$@"
