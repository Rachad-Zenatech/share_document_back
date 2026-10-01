#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export APP_ENV=development

if [ -d "venv" ]; then
    PYTHON_CMD="./venv/bin/python"
elif [ -d ".venv" ]; then
    PYTHON_CMD="./.venv/bin/python"
else
    PYTHON_CMD="python3"
fi

PORT="${PORT:-8900}"
exec $PYTHON_CMD -m uvicorn server:app --host 0.0.0.0 --port "$PORT" --reload
