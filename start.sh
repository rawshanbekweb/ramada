#!/usr/bin/env bash
# macOS / Linux: ./start.sh   (Python 3.11+ kerak)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Birinchi ishga tushirish: kutubxonalar o'rnatilmoqda, bir necha daqiqa..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[edge]"
fi
exec .venv/bin/python -m tools.run_local "$@"
