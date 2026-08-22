# v0.8 Validation

Implemented features:

1. Flashblock selective source-transaction feed using Base `newFlashblockTransactions` with `full=true`, extracting only logs for `WATCHED_POOL_ADDRESSES` and optional `WATCHED_TOPIC0S`. Existing `pendingLogs` remains the efficient default/fallback.
2. Integer-only adaptive submission threshold based on observed trigger age + expected execution latency, calibrated survival window, inclusion probability, expected failure cost, and safety margin.
3. Python enforces the gate after exact packed transaction simulation and exact gas/L1-fee conversion.
4. Rust recomputes the adaptive gate immediately before its exact pending-state `eth_call` and before any signing/submission.

Validation in the packaging runtime:

- Python full suite: 21 passed.
- Python `compileall`: passed.
- Shell `bash -n`: passed.
- Rust unit tests were added for adaptive decay and watched-pool filtering, but Cargo was unavailable in the packaging runtime and therefore was **not run**.
- Solidity v0.7 packed executor behavior was not changed in v0.8. Foundry was unavailable in the packaging runtime and therefore was **not run**.

On Linux/Codex run:

```bash
cp .env.example .env
make setup
make codex-check
make check
```

For the optional full Flashblock transaction stream, set a Flashblocks-aware WebSocket provider and then:

```bash
FLASHBLOCK_TX_STREAM_ENABLED=true
WATCHED_POOL_ADDRESSES=0xPool1,0xPool2
```

Do not use an empty watched-pool list with the full transaction stream. Keep `DRY_RUN=true` until Cargo/Foundry tests, pinned Base fork tests, and shadow-mode calibration pass.
