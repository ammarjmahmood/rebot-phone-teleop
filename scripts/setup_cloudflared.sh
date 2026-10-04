#!/usr/bin/env bash
set -euo pipefail
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l) ARCH=arm ;;
  *) echo "Unsupported architecture $(uname -m)"; exit 1 ;;
esac
mkdir -p "$HOME/.local/bin"
curl -fsSL -o "$HOME/.local/bin/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
chmod +x "$HOME/.local/bin/cloudflared"
"$HOME/.local/bin/cloudflared" --version
echo "Start the server with --cloudflare to get an https address that works from anywhere."
