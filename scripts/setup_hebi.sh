#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv venv .venv-hebi --python 3.11
uv pip install --python .venv-hebi/bin/python "hebi-py==2.16.1"
echo "HEBI bridge ready. Open HEBI Mobile I/O on the phone, then press Start HEBI on the control page."
