# Base Arbitrage Bot v0.5

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
EXTERNAL_SIGNER_BEARER_TOKEN_FILE=/secure/path/gateway-token
EXECUTOR_OWNER_ADDRESS=...
```

The Rust service prepares current EIP-1559 fee fields and delegates signing to the isolated signer documented in `docs/EXTERNAL_SIGNER.md`, then broadcasts only the returned signed transaction. The included local Clef gateway is documented in `signer-gateway/README.md`.

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
curl http://localhost:8085/health
```

## Base Flashblocks shadow mode

The authenticated Base HTTP provider can be reused as its matching WebSocket endpoint without duplicating the credential:

```env
BASE_FLASHBLOCKS_WS=from-base-http-rpc
```

Rust changes only the URL scheme from `https` to `wss`. The configured `pendingLogs` subscription is restricted to the chain-verified WETH/USDC Uniswap V3 and Aerodrome pools in `WATCHED_POOL_ADDRESSES`, and to the two exact Swap signatures in `WATCHED_TOPIC0S`. `WATCHED_POOLS_JSON` must describe exactly the same address set or Python startup fails.

The production shadow gate is one hour by default:

```env
SHADOW_OBSERVATION_SECONDS=3600
SHADOW_POLL_INTERVAL_SECONDS=5
```

With the dry-run services and PostgreSQL running, execute:

```bash
make shadow-observe
```

Reports are written beneath `reports/shadow/` and record pending events, route candidates, rejection reasons, deterministic net-profit values, exact-simulation results, evaluation latency, provider-disagreement measurements, would-submit decisions, and any non-dry submission. Runs shorter than the configured duration are rejected unless explicitly invoked with `--smoke`; smoke reports never satisfy the duration gate.

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

Pinned Base fork integration tests use real Aave, Aerodrome, Uniswap, WETH, and USDC contracts at block `50,048,341` (hash `0x113715da2993561eca7da017a159cc3ca24fc83f0a1e66632ac6447ed5ea7814`):

```bash
make fork-test
```

Prepare an unsigned deployment plan after compiling and configuring a funded external signer address:

```bash
set -a
source .env
set +a
python scripts_prepare_deployment.py \
  --nonce <pending-signer-nonce> \
  --output /secure/path/deployment-plan.json
```

The planner never signs or broadcasts. The executor deploys paused, commitments cannot be replayed, and ownership transfer requires acceptance by the pending owner. Review and submit the plan through the Clef workflow in `signer-gateway/README.md`; live trading remains disabled during deployment.

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
