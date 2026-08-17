SHELL := /usr/bin/env bash
.PHONY: help setup python-test signer-test python-static rust-test solidity-test fork-test shadow-observe check codex-check

help:
	@printf '%s\n' \
	  'make setup         - create Python venv/install dev dependencies' \
	  'make python-test   - run Python tests' \
	  'make signer-test   - run external signer policy tests' \
	  'make python-static - compile Python source' \
	  'make rust-test     - cargo test + clippy' \
	  'make solidity-test - forge build + tests' \
	  'make fork-test     - pinned real Base protocol tests (requires .env RPC)' \
	  'make shadow-observe - enforce and record the configured sustained dry-run window' \
	  'make codex-check   - non-interactive Linux/Codex preflight' \
	  'make check         - run all available checks; missing toolchains reported clearly'

setup:
	@bash scripts/setup_linux.sh

python-test:
	@cd python && if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest -q; else python3 -m pytest -q; fi
	@$(MAKE) --no-print-directory signer-test

signer-test:
	@if [ -x python/.venv/bin/python ]; then PYTHONPATH=signer-gateway/src python/.venv/bin/python -m pytest -q signer-gateway/tests; else PYTHONPATH=signer-gateway/src python3 -m pytest -q signer-gateway/tests; fi

python-static:
	@cd python && if [ -x .venv/bin/python ]; then .venv/bin/python -m compileall -q src tests; else python3 -m compileall -q src tests; fi
	@python3 -m compileall -q signer-gateway/src signer-gateway/tests scripts_prepare_deployment.py scripts_deploy_via_signer.py

rust-test:
	@command -v cargo >/dev/null || { echo 'ERROR: cargo not installed. Run scripts/setup_linux.sh or install Rust.' >&2; exit 127; }
	@cd rust-executor && cargo test && cargo clippy --all-targets --all-features -- -D warnings

solidity-test:
	@command -v forge >/dev/null || { echo 'ERROR: forge not installed. Install Foundry, then rerun.' >&2; exit 127; }
	@cd contracts && forge build && forge test --no-match-path 'test/BaseMainnetFork.t.sol' -vvv

fork-test:
	@command -v forge >/dev/null || { echo 'ERROR: forge not installed. Install Foundry, then rerun.' >&2; exit 127; }
	@test -f .env || { echo 'ERROR: .env missing.' >&2; exit 2; }
	@set -a; . ./.env; set +a; cd contracts && forge test --match-path 'test/BaseMainnetFork.t.sol' -vvv

shadow-observe:
	@test -f .env || { echo 'ERROR: .env missing.' >&2; exit 2; }
	@set -a; . ./.env; set +a; python/.venv/bin/python scripts_shadow_observe.py

codex-check:
	@bash scripts/codex_check.sh

check: python-test python-static
	@status=0; \
	if command -v cargo >/dev/null; then $(MAKE) rust-test || status=$$?; else echo 'NOT RUN: Rust checks (cargo missing)'; fi; \
	if command -v forge >/dev/null; then $(MAKE) solidity-test || status=$$?; else echo 'NOT RUN: Solidity checks (forge missing)'; fi; \
	exit $$status
