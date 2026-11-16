#!/bin/sh
# Run from this script's own project directory, always within lkn_workspace on server.
set -eu
cd "$(dirname "$0")"
python3 -m lxtrace serve --directory artifacts/v2 --port "${1:-8765}"
