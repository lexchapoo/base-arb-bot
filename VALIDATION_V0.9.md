# v0.9 Validation

Added automatic verified Base pool discovery and dynamic watched-pool generation.

## Discovery
- Aerodrome V2 pools are enumerated from the configured Base PoolFactory `allPools` index.
- Uniswap V3 pools are reconstructed only from `PoolCreated` logs emitted by the configured factory.
- Every discovered pool must have deployed bytecode.
- Uniswap pools are re-verified through on-chain `factory()`, `token0()`, `token1()`, and `fee()` reads.
- Optional `POOL_DISCOVERY_TOKEN_ALLOWLIST` can bound the registry without fabricating market state.
- Manual `WATCHED_POOL_ADDRESSES` entries are additive overrides, not required.

## Rust watcher
- Pulls `/pools/addresses` from the Python registry.
- Merges discovered pools with manual addresses.
- Refreshes the registry periodically and reconnects subscriptions when the set changes.
- Splits large address sets into bounded `pendingLogs` subscription chunks.
- Full Flashblock transaction mode uses the same dynamic verified watch set.

## Validation executed in this build environment
- Python unit tests: 24 passed.
- Python compileall: passed.
- Shell syntax/preflight: passed.
- Cargo/Rust tests: not run because Cargo is unavailable in this runtime.
- Foundry/Solidity tests: not run because Foundry is unavailable in this runtime.

Run `make codex-check` and `make check` on the Linux host before funded execution.
