SHELL := /usr/bin/env bash
.PHONY: help setup python-test python-static rust-test solidity-test check codex-check

help:
	@printf '%s\n' \
	  'make setup         - create Python venv/install dev dependencies' \
	  'make python-test   - run Python tests' \
	  'make python-static - compile Python source' \
	  'make rust-test     - cargo test + clippy' \
	  'make solidity-test - forge build + tests' \
	  'make codex-check   - non-interactive Linux/Codex preflight' \
	  'make check         - run all available checks; missing toolchains reported clearly'

setup:
	@bash scripts/setup_linux.sh

python-test:
	@cd python && if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest -q; else python3 -m pytest -q; fi

python-static:
	@cd python && if [ -x .venv/bin/python ]; then .venv/bin/python -m compileall -q src tests; else python3 -m compileall -q src tests; fi

rust-test:
	@command -v cargo >/dev/null || { echo 'ERROR: cargo not installed. Run scripts/setup_linux.sh or install Rust.' >&2; exit 127; }
	@cd rust-executor && cargo test && cargo clippy --all-targets --all-features -- -D warnings

solidity-test:
	@command -v forge >/dev/null || { echo 'ERROR: forge not installed. Install Foundry, then rerun.' >&2; exit 127; }
	@cd contracts && forge build && forge test --no-match-path 'test/*Fork.t.sol' -vvv

codex-check:
	@bash scripts/codex_check.sh

check: python-test python-static
	@status=0; \
	if command -v cargo >/dev/null; then $(MAKE) rust-test || status=$$?; else echo 'NOT RUN: Rust checks (cargo missing)'; fi; \
	if command -v forge >/dev/null; then $(MAKE) solidity-test || status=$$?; else echo 'NOT RUN: Solidity checks (forge missing)'; fi; \
	exit $$status
