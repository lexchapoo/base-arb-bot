# Solidity deployment security review

Review date: 2026-08-16

## Controls verified

- The executor deploys paused and activation remains an explicit owner action.
- Ownership transfer is two-step (`transferOwnership` then `acceptOwnership`).
- Successful route and packed-batch commitments cannot be replayed.
- A top-level operation lock blocks nested execution and control mutation during token, adapter, router, and Aave calls.
- The Aave callback validates pool, initiator, active commitment, route hash, asset, amount, target block, and deadline.
- Packed candidate fallback uses an external self-call so failed candidate state is reverted before the next candidate.
- Adapters accept calls only from the immutable executor, use fixed protocol-specific selectors, approve exact amounts, clear approvals, and verify recipient balance deltas.
- Intermediate tokens must have zero pre-existing executor balance, preventing unrelated balances from being consumed by a route.

## Slither triage

Slither reports `reentrancy-balance` on balance reads around token/router calls. This detector is explicitly excluded from the final severity gate after manual review because balance deltas are the intended output authority and the following independent controls bound reentrancy:

1. `operationLock` is set before any route validation token call and remains set through Aave repayment.
2. `callbackEntered` blocks nested Aave callbacks.
3. `attemptPackedCandidate` is restricted to `onlySelf`.
4. Management and sweep functions require `onlyIdle`.
5. Adapters require the immutable executor as caller and expose only fixed router operations.
6. Failing calls revert the entire route or isolated packed candidate, restoring commitment and allowance state.

The dedicated invariant campaign executes 128,000 unauthorized pause/ownership attempts. Pinned Base fork tests execute actual WETH/USDC swaps through deployed Uniswap and Aerodrome routers, execute and repay a real Aave flash loan, reject an underfunded repayment, and reject an unprofitable real-protocol cycle.

The final Slither gate excludes only `reentrancy-balance`; all other high-impact detectors remain enabled.
