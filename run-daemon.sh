#!/bin/zsh
set -eu
ROOT="${0:A:h}"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m deepseek_bridge.daemon
