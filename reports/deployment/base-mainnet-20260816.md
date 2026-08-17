# Base Mainnet deployment report — 2026-08-16

## Outcome

- Chain: Base Mainnet (`8453`)
- Deployment plan SHA-256: `0x0527b10e4b4cef8366311b45b3e8db0d0f3ddec1a8a0e66c6096c1d01896bb9c`
- Deployer/owner: `0x341938C405773Fc8aE3980c07cA5A516855ED7a2`
- Executor: `0xAb781137345E28944b11C102d7A231B3125f9bA2`
- Aerodrome adapter: `0x417f6c0bEFecb1375EEa19A14Aa193F52C304b69`
- Uniswap V3 adapter: `0x5fd5eE7E77CF7Dc98861e2Dbc9D82bd80406749E`
- Final safety state: executor paused; `DRY_RUN=true`; `LIVE_TRADING=false`
- Signer state after deployment: deployment signing disabled; execution signing disabled; gateway stopped

## Confirmed transactions

| Nonce | Purpose | Transaction | Gas used |
|---:|---|---|---:|
| 0 | Deploy executor | `0x68407362a40ff6187f83a0b911bbdcc25fde99b9d810fcbd0ddd81eaff6b46f5` | 3,758,333 |
| 1 | Deploy Aerodrome adapter | `0xb3ceb466f253dc90333c1316315d64cdfb9359520a7fa5ea2d3251ff2c8415e2` | 619,240 |
| 2 | Deploy Uniswap V3 adapter | `0x6226d0ce68699a131c34ed1f8b85ba1eb4fe8d67b943ed431ba54418ffd9a7ad` | 626,054 |
| 3 | Allow Aerodrome adapter | `0x4572706339deab91f24e57c849ad1297dc1e538530651b8eeb5f69eb36075121` | 54,315 |
| 4 | Allow Uniswap V3 adapter | `0xa2d9e72549719efe643ff7719d41fc0812d5fa0821b34de541958e73a466637f` | 54,315 |
| 5 | Allow WETH | `0xf97852bd5234cbbe8fb15497249e83d3e922ff0632f2976fdd019f3f86dde842` | 54,132 |
| 6 | Allow USDC | `0x4fb7e8cf12bdae44868ef27f3751be4e479b4c8fec18cbaad99a37c774f0b4f4` | 54,348 |

All receipts had status `1`. Total gas used was 5,220,737 at an effective gas price of 6,000,000 wei, for 31,324,422,000,000 wei (`0.000031324422 ETH`).

## Post-deployment verification

- Runtime bytecode exists at the executor and both predicted adapter addresses.
- Executor owner and Aave pool match the reviewed deployment inputs.
- Executor remains paused.
- Both adapter allowlist entries are enabled.
- WETH and USDC token allowlist entries are enabled.
- Aerodrome adapter executor/router/factory bindings match the reviewed constructor inputs.
- Uniswap V3 adapter executor/router/router02 bindings match the reviewed constructor inputs.
- Configured market dependencies, watched pools, pool metadata, and watched topics passed `scripts_verify_runtime.py` against chain state.

## Validation

The required validation sequence completed successfully before continuation:

1. `make python-test` — 22 engine tests and 6 signer-gateway tests passed.
2. `make python-static` — passed.
3. `make rust-test` — 3 tests passed; Clippy completed.
4. `make solidity-test` — 8 tests passed.
5. `make check` — passed (with pre-existing informational Forge lint notes).

## Rollback and next gate

No rollback was required. The immediate containment state is already active: the executor is paused and both signer capabilities are disabled. If configuration rollback becomes necessary, keep the executor paused and submit reviewed owner transactions setting both adapter and token permissions to `false`.

Live/canary activation is not authorized by this deployment. A sustained shadow-mode observation period and its acceptance criteria must complete before any canary or unpause decision.
