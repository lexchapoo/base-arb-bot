# Base Arbitrage Bot v0.5

## v0.8 execution upgrades

- Flashblock-triggered selective evaluation remains scoped to registered affected pools. The Rust service supports Base `pendingLogs` by default and an optional `newFlashblockTransactions` full-data stream (`FLASHBLOCK_TX_STREAM_ENABLED=true`) to extract watched pool logs directly from preconfirmed source transactions. Unknown/unwatched pools do not trigger a global rescan.
- Packed batches now pass an integer-only adaptive submission gate after exact pending-state simulation. The gate discounts deterministic net profit by opportunity survival probability and inclusion probability, subtracts expected failure cost, and requires the result to exceed a configured safety margin.
- Rust recomputes the adaptive gate at the final submission boundary before its own exact pending-state simulation, preventing stale/manual requests from bypassing the Python policy check.

Adaptive formula:

```text
survival_bps = max(0, (survival_window_ms - total_latency_ms) * 10000 / survival_window_ms)
expected_capture = deterministic_net_profit * survival_bps/10000 * inclusion_bps/10000 - failure_cost
submit only when expected_capture > safety_margin
```

Keep the survival window, inclusion probability, failure cost, and safety margin calibrated from real shadow/live observations; do not treat the defaults as validated market constants.

Simulation-first Base atomic-arbitrage/searcher architecture.

## Implemented pipeline

```text
Base Flashblocks pendingLogs
→ Rust event ingestion + deduplication
→ Python affected-subgraph discovery
→ bounded 2/3-hop cycles
→ live pending-state DEX quotes
→ live size search + final re-quote
→ strict executor leg construction
→ current Aave premium + available-liquidity check
→ exact BaseArbExecutor.start(...) calldata
→ Base eth_simulateV1 against pending Flashblock state
→ eth_estimateGas against pending state
→ current gas price
→ Base GasPriceOracle L1 fee upper bound
→ live WETH-to-route-asset gas conversion
→ deterministic post-cost net profit
→ submission eligibility
→ Rust dry-run or externally signed transaction boundary
```

No missing production market value is silently replaced with a mock value. When required configuration, liquidity, token metadata, gas conversion liquidity, or protocol state is unavailable, the route receives an explicit blocker and is not submission eligible.

## Solidity

`BaseArbExecutor` now uses constrained typed swap legs instead of arbitrary target calls.

Security properties include:

- owner-only route start
- route hash commitment
- token allowlist
- adapter allowlist
- Aave callback + initiator authentication
- exact target-block freshness gate
- clean intermediate-token requirement
- exact per-leg pending quote used as `minOut`
- adapter output-delta verification
- allowance reset after every adapter call
- flash-loan repayment invariant
- net-profit invariant measured after Aave premium
- pause control
- no `delegatecall`
- no unrestricted target calldata forwarding

Included adapters:

- `AerodromeAdapter`
- `UniswapV3Adapter` supporting legacy v3 router and SwapRouter02 tuple formats

Deployment addresses are intentionally not guessed. Configure verified current deployments through environment variables.

## Base fee accounting

The finalizer uses both Base fee components:

1. L2 execution cost from simulated/estimated gas and current RPC gas price.
2. L1 security fee using Base's `GasPriceOracle.getL1FeeUpperBound(txSize)`.

For EIP-1559 transaction size, the code constructs a conservative serialized-size bound using the actual nonce, fee fields, gas limit, destination, and calldata with maximum-width signature scalars.

Native ETH-denominated costs are converted to the route's starting asset through a live pending-state WETH/asset quote. If no suitable configured live conversion pool exists, execution is blocked rather than inventing an exchange rate.

## Aave

The finalizer queries the configured Aave Pool against `pending` state for:

- `FLASHLOAN_PREMIUM_TOTAL()`
- reserve configuration bit 63 (flash-loan enabled)
- current aToken address
- current underlying balance held by the aToken

The final `eth_simulateV1` call remains the authoritative execution check.

## Automatic dry-run submission

When:

```env
DRY_RUN=true
AUTO_SUBMIT_ROUTES=true
```

positive fully simulated routes are sent automatically to the Rust `/submit-plan` endpoint, which performs another pending-state `eth_call` against the exact executor calldata.

No transaction is broadcast in dry-run mode.

## Live signing boundary

The application never accepts a raw private key.

Live broadcasting requires all of:

```env
DRY_RUN=false
LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND_MAINNET_RISK
EXTERNAL_SIGNER_URL=...
EXTERNAL_SIGNER_ADDRESS=...
EXECUTOR_OWNER_ADDRESS=...
```

The Rust service prepares current EIP-1559 fee fields and delegates signing to the isolated signer documented in `docs/EXTERNAL_SIGNER.md`, then broadcasts only the returned signed transaction.

## Configuration

```bash
cp .env.example .env
```

Required runtime deployments remain blank in `.env.example` on purpose. Fill them only after resolving them from authoritative protocol deployment sources and verifying bytecode on Base.

Verify configured contracts:

```bash
set -a
source .env
set +a
python scripts_verify_runtime.py
```

## Run

```bash
docker compose up --build
```

Health endpoints:

```bash
curl http://localhost:8080/health
curl http://localhost:8081/health
```

## Tests

Python:

```bash
python -m pytest -q python/tests
```

Solidity:

```bash
cd contracts
forge test -vvv
```

The Solidity suite includes repayment/net-profit and route-hash mutation tests using isolated test fixtures. Real protocol validation must additionally be run against a Base mainnet fork with verified current deployment addresses.

## Current validation status

The artifact was validated in the build environment as follows:

- Python unit tests: executed
- Python bytecode compilation: executed
- Rust compilation: not executable in the artifact-build environment because Cargo was unavailable
- Solidity compilation/Foundry tests: not executable in the artifact-build environment because Foundry/solc was unavailable
- live Base route execution: not performed
- funded transaction submission: not performed

Do not interpret unexecuted Rust/Solidity validation as passing.

## Packed multi-route execution (v0.6)

`BaseArbExecutor.startPacked(bytes)` accepts up to 8 pre-ranked candidate cycles sharing the same settlement asset, flash-loan amount, target block, and deadline. The contract takes one Aave flash loan and tries candidates in order. Each candidate is executed through an external self-call, so a failed swap/min-out/profit check reverts that candidate's nested DEX state before the executor tries the next candidate. The first candidate that covers the Aave premium plus its committed minimum profit is selected; if none succeeds, the entire transaction reverts.

Packed payload (big-endian):

```text
version:u8 | candidateCount:u8 | asset:20 | amount:u256 | targetBlock:u64 | deadline:u64 |
  candidateId:bytes32 | minProfit:u256 | legCount:u8 |
    adapter:20 | tokenIn:20 | tokenOut:20 | minOut:u256 | dataLen:u16 | data:bytes
```

Bounds are enforced on-chain: 8 candidates maximum, 4 legs per candidate, and 2048 bytes of adapter data per leg. Python implements the same codec in `arb_bot.packed_batch`; Rust implements the same encoder/validator and exposes `POST /pack-batch`. Live submission must still simulate the final `startPacked` calldata and remain behind the existing dry-run/manual activation gates.

## v0.7 packed automatic execution path

`/route-trigger` now evaluates affected cycles, forms groups that share the exact flash-loan asset/amount/target-block/deadline, ranks each compatible group by deterministic net profit, and constructs at most one packed batch for automatic submission. It never combines routes that optimized to different flash-loan amounts.

Before calling Rust, Python simulates the exact final `startPacked(bytes)` transaction, estimates gas, includes the Base L1 fee upper bound, converts native execution cost into the settlement asset, and requires every retained fallback candidate to conservatively cover that packed transaction cost. Low-profit fallback candidates are removed from the tail until the packed menu passes the cost gate or no viable menu remains.

The Rust `/submit-plan` boundary independently performs `eth_call` on the exact final calldata immediately before either dry-run acceptance or live external signing. This second simulation cannot be bypassed by the Python planner. The Solidity executor still performs flash-loan repayment and per-candidate minimum-profit enforcement.

### Codex on Linux

From the repository root:

```bash
cp .env.example .env
make setup
make codex-check
make check
```

Codex should read `AGENTS.md` before editing. `make check` runs Python checks and runs Rust/Foundry checks when those toolchains are installed; missing toolchains are reported as `NOT RUN` rather than treated as successful. Keep `DRY_RUN=true` while validating the deployment.

## v0.9 automatic pool discovery

Manual `WATCHED_POOL_ADDRESSES` population is no longer required. On startup, the Python service discovers and verifies Base pools from the configured Aerodrome and Uniswap V3 factories and exposes the resulting registry at `GET /pools/addresses`. The Rust Flashblocks watcher merges this live registry with any manual overrides and automatically rebuilds its filtered subscriptions when the registry changes.

Useful controls:

```env
AUTO_POOL_DISCOVERY_ENABLED=true
POOL_DISCOVERY_REFRESH_SECONDS=60
POOL_DISCOVERY_FROM_BLOCK=0
POOL_DISCOVERY_LOG_CHUNK_BLOCKS=20000
POOL_DISCOVERY_TOKEN_ALLOWLIST=
PYTHON_POOL_REGISTRY_URL=http://python-api:8080/pools/addresses
WATCH_POOL_REFRESH_SECONDS=30
WATCH_POOL_SUBSCRIPTION_CHUNK_SIZE=200
```

`POST /pools/refresh` forces a discovery refresh. `GET /pools` shows full metadata; `GET /pools/addresses` returns the exact address set consumed by Rust.

## v0.10 persistent pool discovery

Verified pool metadata and factory discovery cursors are persisted in PostgreSQL. On restart, the Python API restores the verified registry into the live graph before scanning forward. Uniswap V3 resumes from the last atomically committed block cursor, and Aerodrome resumes from the last committed `allPools` index. Existing deployments are upgraded automatically by SQLAlchemy `create_all`; fresh PostgreSQL volumes also receive the tables through `sql/init.sql`.


## v0.11 state/telemetry layer

- Rust keeps an event-backed local state fingerprint/cache for every watched pool.
- Every route trigger carries a state version and monotonic sequence.
- Submission fails closed if market state advances before final simulation/signing.
- `BASE_HTTP_RPCS` supports multiple independent Base HTTPS providers; critical simulation requires agreement when `RPC_CONSENSUS_REQUIRED=true`.
- Every evaluated trigger is persisted to `opportunity_telemetry` for replay and missed-opportunity analysis.
- Live receipts store exact gas used, effective gas price, Base `l1Fee` when present, and settlement-token balance deltas.
- Cross-asset gas conversion is never invented; exact net asset PnL is marked only when conversion is mathematically exact.

Useful APIs:

```bash
curl http://localhost:8080/telemetry/opportunities
curl http://localhost:8080/telemetry/replay/<opportunity_id>
curl http://localhost:8081/health
```

## v0.12 — telemetry-driven shadow intelligence

The strategy API now computes empirical, observation-backed intelligence without placing ML in the transaction-control path:

- route-family capture probability from completed receipt outcomes;
- pool-state survival probability from observed inter-event lifetimes;
- Flashblock sender/multi-pool fingerprints for competitor analysis; and
- a shadow `NOW / WAIT / SKIP` recommendation persisted beside the real deterministic decision.

No probability is emitted until `SHADOW_MIN_SAMPLES` real observations exist. The shadow recommendation never changes packed calldata, route ordering, signing, or submission.

Useful endpoints:

```bash
curl http://localhost:8080/intelligence/shadow
curl http://localhost:8080/intelligence/competitors
```
