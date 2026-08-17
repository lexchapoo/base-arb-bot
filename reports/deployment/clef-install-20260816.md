# Clef installation report — 2026-08-16

## Provenance

- Upstream: `https://github.com/ethereum/clef`
- Pinned commit: `5924e70366fedd61b7637eb3f06e2aa0f0522185`
- Embedded module version: `v0.0.0-20260602211849-5924e70366fe`
- Build toolchain: official Go `1.26.6` Linux AMD64 archive
- Go archive SHA-256: `708effb774be8237570d0add163225abbdfaf4fca28b2611df167beba4feef89`
- Installed binary: `/home/shiesty/.local/bin/clef`
- Installed binary SHA-256: `2dd3aab146ebd464c15be60685bc989f06e426e357560bd23712a80e4a6e5bdc`

Geth 1.17.5's official all-tools bundle was separately downloaded and its detached
signature verified against the fingerprint published on the Geth downloads page:
`FDE5 A1A0 44FA 13D2 F7AD A019 A61A 1356 9BA2 8146`. That bundle does not contain
Clef because upstream moved Clef to the standalone repository in Geth 1.17.4.

## Validation

- Official standalone Clef repository full Go test suite: passed
- Pinned Clef binary build: passed
- Installed binary version check: `Clef version v1.0.0`
- Embedded Go module provenance inspection: passed

## Configuration state

- Active signer address: `0x341938C405773Fc8aE3980c07cA5A516855ED7a2`
- Keystore directory: `/home/shiesty/.ethereum/keystore` (`0700`); encrypted key file is `0600`
- Clef config directory: `/home/shiesty/.clef` (`0700`); encrypted master seed is `0400`
- Gateway token: generated outside the repository with `0600` permissions; value not logged
- Root `EXTERNAL_SIGNER_BEARER_TOKEN_FILE`: configured to the token path
- Clef initialization/account creation: completed interactively by the operator
- `DRY_RUN=true`
- `LIVE_TRADING=false`
- Deployment signing: disabled
- Execution signing: disabled
- Funded transaction submission: not performed
- Base Mainnet balance at configuration check: `0 wei`
- Base Mainnet pending nonce at configuration check: `0`
- Reviewed deployment plan: `/home/shiesty/.local/share/base-arb-signer/deployment-plan.json` (`0600`)
- Deployment plan SHA-256: `0527b10e4b4cef8366311b45b3e8db0d0f3ddec1a8a0e66c6096c1d01896bb9c`
- Predicted paused executor: `0xAb781137345E28944b11C102d7A231B3125f9bA2`
- Predicted Aerodrome adapter: `0x417f6c0bEFecb1375EEa19A14Aa193F52C304b69`
- Predicted Uniswap V3 adapter: `0x5fd5eE7E77CF7Dc98861e2Dbc9D82bd80406749E`
- Funding transaction: `0x2842a18c62ebdd7f0d2d7566d402ba85d85fd06d92d6b754965e1e8fc09bce14`
- Funding receipt: successful on Base Mainnet
- Funded amount: `0.001062920340848560 ETH`
- Deployment preview: successful; no signature requested and no transaction broadcast
- Conservative total gas limit: `6,164,631`
- Preview-time maximum fee exposure: approximately `0.00006781 ETH`
- Deployment gas policy: separate `6,000,000` per-transaction cap; execution remains capped at `3,000,000`

## Activation and rollback

Initialization and account creation must be performed manually because Clef displays
recovery material and requests secret passwords. Deployment remains blocked until the
operator reviews the generated account, funds only its deployment gas, creates and
reviews the exact deployment plan, and separately enables deployment signing.

Rollback is immediate: stop Clef and the gateway, keep both signing flags false, and
leave the on-chain executor paused. Removing the bearer-token path from `.env` also
disconnects the Rust executor from the signer gateway.
