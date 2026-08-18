# Contributing

Thanks for your interest in improving the Base arbitrage bot. This project
handles real on-chain execution logic, so contributions are held to a
safety-first bar. Please read this before opening a pull request.

## Ground rules (non-negotiable)

These mirror `AGENTS.md` and are enforced in review:

- **Dry-run stays the default.** Never change `DRY_RUN=true` in `.env.example`,
  and never enable live trading in a PR.
- **No raw private keys.** Signing goes through the external signer interface
  only (`docs/EXTERNAL_SIGNER.md`, `signer-gateway/`). Do not add key handling.
- **Do not weaken safety checks.** Target-block freshness, deadlines, token and
  adapter allowlists, flash-loan repayment, per-candidate minimum profit, and
  exact-simulation gates must not be loosened, bypassed, or made optional.
- **No fabricated market state in production paths.** If required config,
  liquidity, token metadata, or protocol state is missing, emit an explicit
  blocker — never a mock or invented value. Deterministic test fixtures are fine
  in unit tests only; Base fork/live validation must use real chain state.
- **Integer math in execution.** Production monetary arithmetic uses raw integer
  token units. No float math in execution decisions.
- **Layered authority.** Python may rank/group candidates; Rust re-simulates the
  exact final calldata before accepting/submitting; Solidity remains the final
  profit/repayment authority. Keep that separation intact.

## Development setup

```bash
cp .env.example .env       # fill deployments only from verified sources
make setup                 # Python venv + dev deps
```

Leave the runtime deployment addresses blank until you have verified current
Base deployments and their bytecode. Never commit `.env` or any secret.

## Validating a change

Run, in order (from `AGENTS.md`):

```bash
make python-test     # pytest + external-signer policy tests
make python-static   # compileall
make rust-test       # cargo test + clippy -D warnings (needs Cargo)
make solidity-test   # forge build + tests (needs Foundry)
make check           # runs all available; missing toolchains report NOT RUN
```

If a toolchain is unavailable, report it as `NOT RUN` — never describe an
unexecuted suite as passing. Real protocol changes should also be exercised
against a Base mainnet fork:

```bash
make fork-test       # pinned real Aave/Aerodrome/Uniswap/WETH/USDC state
```

## Code layout

| Area | Path |
|------|------|
| Route/batch planning | `python/src/arb_bot/route_engine.py`, `batch_execution.py` |
| Quote adapters | `python/src/arb_bot/adapters/` |
| Multicall batching | `python/src/arb_bot/multicall.py` |
| Packed codec | `python/src/arb_bot/packed_batch.py`, `rust-executor/src/packed.rs` |
| Rust submission boundary | `rust-executor/src/main.rs` |
| On-chain executor | `contracts/src/BaseArbExecutor.sol` |

Keep files focused and under ~500 lines; validate inputs at system boundaries.

## Pull requests

- Branch from `master`; keep PRs scoped to one change.
- Describe what changed, why, and how you validated it (paste the relevant
  `make` output). State clearly which suites were `NOT RUN`.
- Use clear commit messages (`type: summary`, e.g. `fix:`, `feat:`, `perf:`,
  `docs:`). Do not add secrets, keys, or `.env` files.
- New behavior needs tests. Performance changes should include before/after
  numbers.

## Reporting security issues

Do **not** open a public issue for a vulnerability in the executor, signer, or
execution path. Report it privately to the maintainer (GitHub
[@lexchapoo](https://github.com/lexchapoo)) and allow time to remediate before
any public disclosure.
