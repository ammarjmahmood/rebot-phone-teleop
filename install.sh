#!/usr/bin/env bash
set -euo pipefail
REPO="${REBOT_TELEOP_REPO:-https://github.com/ammarjmahmood/rebot-phone-teleop}"
DIR="${REBOT_TELEOP_DIR:-$HOME/rebot-phone-teleop}"
WITH_HEBI=0
SKIP_CAN=0
for arg in "$@"; do
  case "$arg" in
    --hebi) WITH_HEBI=1 ;;
    --no-can) SKIP_CAN=1 ;;
    *) echo "Unknown option $arg (use --hebi or --no-can)"; exit 1 ;;
  esac
done
say() { printf '\n==> %s\n' "$*"; }
if [ "$(uname -s)" != Linux ]; then
  echo "The arm server runs on Linux (PC, Jetson or Raspberry Pi). Build the iPhone app on a Mac with ios/AGENTS.md."
  exit 1
fi
command -v git >/dev/null || { echo "Install git first: sudo apt install git"; exit 1; }
command -v curl >/dev/null || { echo "Install curl first: sudo apt install curl"; exit 1; }
if ! command -v uv >/dev/null; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only
else
  say "Downloading into $DIR"
  git clone "$REPO" "$DIR"
fi
cd "$DIR"
say "Installing Python 3.11 and the teleop server"
uv venv --allow-existing --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
if [ "$WITH_HEBI" = 1 ]; then
  say "Setting up HEBI Mobile I/O support"
  scripts/setup_hebi.sh
fi
if [ "$SKIP_CAN" = 0 ]; then
  say "Setting up the PEAK USB CAN adapter (asks for your password once)"
  scripts/setup_can.sh || echo "CAN setup did not finish; fix the message above and run scripts/setup_can.sh again"
fi
say "Installed"
cat <<MSG
Start the server:
  cd $DIR && .venv/bin/python -m rebot_teleop --lan

Then open http://127.0.0.1:8080 on this computer, or from another computer:
  ssh -L 8080:localhost:8080 $(whoami)@$(hostname)
  and open http://127.0.0.1:8080 there.
MSG
