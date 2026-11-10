#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== reBot agent setup in $(pwd)"
if [ ! -x .venv/bin/python ]; then
  echo "No .venv here. Run ./install.sh first, then run this again."
  exit 1
fi
extras="agent,camera"
if .venv/bin/python -c "import pybullet" 2>/dev/null; then
  extras="sim,agent,camera"
elif [ "$(uname -m)" = "x86_64" ]; then
  extras="sim,agent,camera"
else
  echo "Skipping the simulator extra on $(uname -m); run the simulator on the PC and point agents at it with --url."
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -e ".[${extras}]"
else
  .venv/bin/pip install -e ".[${extras}]"
fi
.venv/bin/python -c "import rebot_teleop.mcp_server, rebot_teleop.agent, rebot_teleop.dojo; print('robot agent packages ok')"
if command -v claude >/dev/null 2>&1; then
  echo "Claude Code found: $(claude --version 2>/dev/null | head -1)"
else
  echo "Claude Code is not installed. Install it with:"
  echo "  curl -fsSL https://claude.ai/install.sh | bash"
fi
ls /dev/video* 2>/dev/null && echo "Cameras above; pass them with --camera left=/dev/videoN --camera right=/dev/videoM" || echo "No /dev/video devices found"
cat <<'NEXT'

Next:
  1. claude login                       (once; on a headless Jetson it prints a link to open on your phone)
  2. start the robot server             (simulator: .venv/bin/python -m rebot_teleop --sim headless)
  3. claude --model claude-fable-5-2    (from this folder; approve the "rebot" MCP server when asked)
  4. say: read docs/JETSON_NEXT.md and continue
NEXT
