#!/bin/sh
set -eu

COMMAND="${1:-start}"

case "$COMMAND" in
  build)
    python -m pip install --upgrade pip
    python -m pip install --no-cache-dir -r requirements.txt
    ;;
  start)
    exec python bot.py
    ;;
  check)
    python -m py_compile bot.py
    ;;
  *)
    echo "Usage: sh ./setup.sh [build|start|check]"
    exit 1
    ;;
esac
