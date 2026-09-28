#!/usr/bin/env bash
# Local run without Docker: needs a reachable Postgres in DATABASE_URL.
set -euo pipefail
export DATABASE_URL="${DATABASE_URL:-postgresql://postgres@127.0.0.1:5432/csvimport}"
export DATA_DIR="${DATA_DIR:-./data/imports}"
mkdir -p "$DATA_DIR"
python -m app.worker & WORKER=$!
trap 'kill -TERM $WORKER 2>/dev/null; wait $WORKER' EXIT
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
