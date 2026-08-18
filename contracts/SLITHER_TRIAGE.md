# Slither triage

Baseline run (slither 0.11.x, solc 0.8.24): **77 findings**, all triaged to one of
three buckets — test-only mock, guarded by the `operationLock` mutex, or an
intentional by-design pattern. **No genuine vulnerability was found.**

`slither.config.json` filters the test mocks and excludes the detectors listed
below (with the justification here). CI runs Slither with `fail-on: high`, so a
*new* finding outside these triaged categories fails the build. Re-review this
file whenever `BaseArbExecutor.sol` or the adapters change.

## Reentrancy — guarded (34 findings)

`reentrancy-balance` (24, High), `reentrancy-no-eth` (4, Medium),
`reentrancy-benign` (4), `reentrancy-events` (2).

Every external entry point (`start`, `startPacked`) carries the `operationLock`
modifier — a mutex (`operationEntered` flag, `revert Reentrant()`). The Aave
flash callback is callable only by the pool and checks the initiator. Slither
does not recognise the custom modifier as a reentrancy guard, so it flags the
intentional balance reads. Those reads are a **security feature**: each leg
re-measures `balanceOf` before/after the adapter swap and requires
`reported == delta`, so a lying or reentrant adapter is rejected.

## Test mock (1 High + optimization)

`arbitrary-send-erc20` in `MockPool.flashLoanSimple` (`src/mocks.sol`) and the
mock immutable-state hints. Mocks are test-only and never deployed; excluded via
`filter_paths`.

## By-design patterns

- `incorrect-equality` (2) — `reported != delta` deliberately requires the
  adapter's reported output to exactly equal the measured balance delta.
- `unused-return` (4) — the executor ignores the adapter's returned amount and
  trusts the measured balance delta / the flash-repayment + min-profit invariant
  (`balanceOf < preLoan + owed + minProfit` reverts) as the source of truth.
- `uninitialized-local` (4) — default values are intended: `bool success` starts
  false and gates `NoCandidateSucceeded`; the decode offset starts at 0; the
  `legsHash` accumulator seeds from `bytes32(0)`.
- `calls-loop` (13) — external calls inside the leg loop (≤4 legs) and packed
  candidate loop (≤8 candidates); both bounds are enforced on-chain, so there is
  no unbounded-gas / DoS vector.
- `timestamp` (4) — `block.timestamp` is used only for deadline/expiry checks.

## Informational / optimization

`low-level-calls` (safe-approve/transfer via `.call` for non-standard ERC20s),
`assembly` (packed-payload decoding), `naming-convention`, and immutable-state
optimizations are excluded via `exclude_informational` / `exclude_optimization`.
