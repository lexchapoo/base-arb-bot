# AGENTS.md — Base Arbitrage Bot

## Mission
Maintain a Base-mainnet arbitrage engine with deterministic safety, exact transaction simulation, packed multi-route fallback execution, and conservative cost accounting.

## Codex/Linux operating rules
- Work from the repository root.
- Never enable live trading or alter `DRY_RUN=true` unless the user explicitly requests funded execution.
- Never insert mock/fabricated market state into production paths. Unit-test fixtures are allowed only for deterministic codec/control-flow tests; Base fork/live validation must use real chain state.
- Do not weaken target-block, deadline, allowlist, flash-repayment, min-profit, or exact-simulation checks.
- Production monetary arithmetic uses integers/raw token units. Do not introduce float math into execution decisions.
- `startPacked` candidates must share asset, flash-loan amount, target block, and deadline. Never silently mix incompatible candidates.
- The Python layer may rank/group candidates. Rust must re-simulate exact final calldata before accepting/submitting. Solidity remains the final profit/repayment authority.
- Keep `DRY_RUN=true` as the default in `.env.example`.
- Do not add raw private-key handling. Live signing uses the external signer interface only.

## Required validation after code changes
Run, in order:
1. `make python-test`
2. `make python-static`
3. `make rust-test` when Cargo is installed
4. `make solidity-test` when Foundry is installed
5. `make check`

If a tool is missing, report it as NOT RUN; never describe it as passing.

## Key paths
- Python route/batch planning: `python/src/arb_bot/route_engine.py`, `python/src/arb_bot/batch_execution.py`
- Packed codec: `python/src/arb_bot/packed_batch.py`, `rust-executor/src/packed.rs`
- Rust submission boundary: `rust-executor/src/main.rs`
- On-chain executor: `contracts/src/BaseArbExecutor.sol`
- Packed Solidity regression tests: `contracts/test/BaseArbExecutor.t.sol`

## Safe default
Use shadow/dry-run mode and exact pending-state simulation. Never broadcast merely because a private key or signer is configured.
