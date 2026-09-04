#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
command -v python3 >/dev/null || { echo 'python3 is required' >&2; exit 1; }
if command -v uv >/dev/null; then
  cd python
  uv venv --python python3 .venv >/dev/null 2>&1 || true
  uv pip install --python .venv/bin/python -e '.[dev]'
else
  python3 -m venv python/.venv
  python/.venv/bin/python -m pip install --upgrade pip
  (cd python && .venv/bin/python -m pip install -e '.[dev]')
fi
cd "$ROOT"
[ -f .env ] || cp .env.example .env
printf 'Python environment ready. Keep DRY_RUN=true until live validation is complete.\n'
printf 'Optional toolchains: Cargo=%s Forge=%s Docker=%s\n' "$(command -v cargo || echo missing)" "$(command -v forge || echo missing)" "$(command -v docker || echo missing)"

# Solidity dependencies are not vendored into git (contracts/lib is ignored). The upgradeable
# executor needs OpenZeppelin; restore it whenever Foundry is available.
if command -v forge >/dev/null; then
  if [ ! -d contracts/lib/openzeppelin-contracts-upgradeable ]; then
    echo 'Installing Solidity dependencies (openzeppelin-contracts-upgradeable)...'
    (cd contracts && forge install OpenZeppelin/openzeppelin-contracts-upgradeable --no-git)
  fi
else
  echo 'NOT RUN: forge install (Foundry missing) - contracts/ will not build until installed'
fi
