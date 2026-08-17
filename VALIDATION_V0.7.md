# v0.7 Validation

Validated in the build environment on 2026-08-16.

## Passed
- Python test suite: 17 passed.
- Python `compileall`: passed.
- `scripts/setup_linux.sh` shell syntax: passed.
- `scripts/codex_check.sh` shell syntax: passed.
- Codex preflight: passed for all available toolchains.
- Packed codec round-trip/malformed payload tests.
- Compatible grouping does not mix flash-loan amounts.
- Exact packed simulation/cost gate test.
- Low-profit fallback trimming test.
- Existing route-engine, execution-cost, graph, router, and scoring tests.

## Not run in this environment
- Rust `cargo test` / Clippy: Cargo is not installed.
- Solidity `forge build` / `forge test`: Foundry is not installed.
- Docker build: Docker is not installed.

These are NOT passing claims. Run `make check` on the Codex Linux host after installing Cargo and Foundry.
