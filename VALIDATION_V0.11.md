# v0.11 validation

Adds state-versioned opportunities, Rust pool-state cache, multi-RPC selection/consensus, receipt reconciliation fields, and persistent opportunity telemetry/replay inputs.

Run on Linux/Codex:

```bash
make codex-check
make check
```

Required before funded execution: Python tests, Cargo test+clippy, Foundry tests, pinned Base fork tests, two independent healthy Base RPCs, and shadow-mode receipt reconciliation.
