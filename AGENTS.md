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

## v0.8 Flashblock/adaptive execution rules
- Prefer `pendingLogs` for bandwidth-efficient watched-pool triggers. `newFlashblockTransactions` full-data mode is optional and must still filter to `WATCHED_POOL_ADDRESSES` before route evaluation.
- Never turn an unknown transaction/log into a global graph rescan.
- Adaptive submission accounting must use integer raw asset units only.
- Python may pre-gate a batch, but Rust must recompute the adaptive threshold at the final submission boundary.
- Treat survival window, inclusion probability, expected failure cost, and safety margin as calibration inputs from real observations, not invented market truth.
- A stale opportunity that exceeds its survival window must be rejected even if deterministic gross/net profit is otherwise positive.


## v0.11 State-version rule
- Never submit a route if its `state_sequence` differs from the Rust watcher current sequence.
- Critical simulation must agree across independent RPCs when `RPC_CONSENSUS_REQUIRED=true`. That agreement has two axes: sealed-block bytes (always enforced) and the pending state the trade was actually priced on (`RPC_CONSENSUS_PENDING_MODE`, default `observe`). Never report the sealed-block check as agreement about pending state -- the submission response names which axis held.
- Never fabricate native-token-to-settlement-token conversions for realized PnL; persist exact raw fee and token-delta components instead.
- Preserve negative/skipped opportunities in telemetry for replay and calibration.


## v0.12 Shadow-intelligence rules
- `NOW / WAIT / SKIP` is shadow-only. It must never construct calldata, reorder packed routes, sign, broadcast, or bypass deterministic gates.
- Survival calibration and route capture probability must fail closed until `SHADOW_MIN_SAMPLES` real observations exist. Never invent priors and present them as observed probabilities.
- Competitor/searcher fingerprints may use only observed Flashblock transaction senders and watched-pool footprints. Do not infer an identity when `from` is unavailable.
- Keep the real deterministic decision and the shadow recommendation side by side for counterfactual evaluation.
- Do not promote a shadow policy into the execution path without a separate explicit audit/promotion change.

## Test hermeticity
- `Settings` loads `ARB_BOT_ENV_FILE` (default `.env`, resolved against the working directory). `python/tests/conftest.py` points it at a nonexistent path, strips ambient variables matching `Settings` fields, and then **rebuilds the settings object in place** -- importing `arb_bot.config` already ran `settings = Settings()`, so clearing `os.environ` afterwards leaves `os.environ` clean and `settings` still holding the polluted value. The suite gives the same result from the repo root and from `python/`, and with hostile values exported.
- `conftest.py` also points `database_url` at a closed port. `db/session.py` builds its engine at import time from that setting, and the default targets the same localhost Postgres the compose file runs -- so on any machine where that database is up, handlers under test opened real connections and wrote real rows (`route_trigger` alone persists pending events, route evaluations and telemetry). Tests must never require a database; if one starts needing persistence, fake the store, do not point it at a live server.

## Operator-only endpoints
- Anything that can rewrite what the router trades is gated behind `OPERATOR_API_TOKEN` (`Depends(require_operator)`), compared with `hmac.compare_digest`, and fails closed when the token is unset. Adding a new endpoint that mutates the pool graph or the curated set means adding that dependency; read-only endpoints stay open.
- Settings are wired in `_build_pool_discovery()` so the wiring itself is testable. A setting that is defined but never passed is a silent no-op — assert new knobs reach their consumer with a *non-default* value, since comparing two defaults proves nothing.
