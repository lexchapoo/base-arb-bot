# v0.12 Validation — Shadow Intelligence

## Added
- Empirical opportunity/state-survival calibration from observed watched-pool event lifetimes.
- Per-route-family capture probability from completed real receipt outcomes.
- Flashblock transaction sender + multi-pool footprint capture for competitor/searcher fingerprints.
- Shadow-only `NOW / WAIT / SKIP` recommendations stored beside the deterministic execution decision.
- PostgreSQL additive migration for existing v0.11 databases.

## Safety boundary
The shadow policy is advisory only. It cannot construct calldata, reorder packed routes, change flash-loan sizing, sign, submit, or relax Rust/Solidity safety checks. Calibrated probabilities remain `null` until the configured real-observation minimum is reached.

## Required Linux/Codex checks
```bash
make setup
make codex-check
make check
```

Keep `DRY_RUN=true` while validating the new telemetry pipeline.
