#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo "repo=$ROOT"
echo "python=$(command -v python3 || echo missing)"
echo "uv=$(command -v uv || echo missing)"
echo "cargo=$(command -v cargo || echo missing)"
echo "forge=$(command -v forge || echo missing)"
echo "docker=$(command -v docker || echo missing)"
echo "v0.12 features: v0.11 + empirical-survival + route-capture + competitor-fingerprints + shadow-NOW-WAIT-SKIP"
[ -f AGENTS.md ] || { echo 'ERROR: AGENTS.md missing' >&2; exit 1; }
[ -f .env ] || echo 'NOTICE: .env missing; run make setup or cp .env.example .env'
if [ -f .env ] && grep -Eq '^DRY_RUN=(false|False|FALSE|0)$' .env; then
  echo 'WARNING: DRY_RUN is disabled in .env'
fi
python3 -m compileall -q python/src python/tests
if [ -x python/.venv/bin/python ]; then
  python/.venv/bin/python -m pytest -q python/tests
else
  python3 -m pytest -q python/tests
fi
if command -v cargo >/dev/null; then (cd rust-executor && cargo test); else echo 'NOT RUN: cargo test (cargo missing)'; fi
if command -v forge >/dev/null; then (cd contracts && forge build && forge test --no-match-path 'test/BaseMainnetFork.t.sol' -vvv); else echo 'NOT RUN: forge tests (forge missing)'; fi
echo 'Codex preflight complete.'
