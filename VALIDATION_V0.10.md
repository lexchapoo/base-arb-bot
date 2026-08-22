# v0.10 Validation — Persistent Pool Registry

## Added
- PostgreSQL `verified_pools` registry.
- PostgreSQL `discovery_checkpoints` table.
- Startup graph rehydration from verified persisted pools.
- Aerodrome factory-index cursor persistence.
- Uniswap V3 block cursor persistence.
- Atomic pool + checkpoint commits per discovery batch/chunk.
- Factory mismatch protection: a changed factory ignores the old checkpoint.

## Safety semantics
A discovery checkpoint is advanced only in the same DB transaction that stores the verified pools for that batch. If the process, RPC verification, or DB transaction fails before commit, the cursor remains unchanged and the batch is replayed after restart.

## Linux/Codex validation
Run:

```bash
make codex-check
make check
```

Keep `DRY_RUN=true` until all Rust, Solidity, fork, and live-read checks pass.
