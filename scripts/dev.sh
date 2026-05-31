#!/usr/bin/env bash
# One-shot local dev bootstrap. Idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not installed. Run: brew install uv" >&2
  exit 1
fi

echo "==> uv sync"
uv sync --dev

echo "==> docker compose up (Postgres + Redis)"
docker compose -f infra/docker/docker-compose.yml up -d

echo "==> waiting for Postgres"
until docker exec atlas-postgres pg_isready -U atlas -d atlas >/dev/null 2>&1; do
  sleep 1
done
echo "    Postgres ready."

echo "==> running tests"
uv run pytest -q

echo
echo "All set. Start the signal API with:"
echo "  uv run --package signal-service uvicorn signal_service.main:app --reload"
