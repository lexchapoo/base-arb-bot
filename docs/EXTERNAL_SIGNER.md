# External Signer Contract

The Rust executor never accepts raw private keys. This repository includes a local,
loopback-only policy gateway backed by Geth Clef; setup and deployment instructions
are in [`signer-gateway/README.md`](../signer-gateway/README.md).

When `DRY_RUN=false`, `LIVE_TRADING=true`, and the explicit live-risk acknowledgement is set, it POSTs the transaction below to `EXTERNAL_SIGNER_URL`.

Request body:

```json
{
  "chain_id": 8453,
  "from": "0x...",
  "to": "0x...",
  "nonce": "0x...",
  "gas": "0x...",
  "max_fee_per_gas": "0x...",
  "max_priority_fee_per_gas": "0x...",
  "value": "0x0",
  "data": "0x...",
  "route_id": "0x...",
  "asset": "0x..."
}
```

Expected response:

```json
{"raw_transaction":"0x..."}
```

When `EXTERNAL_SIGNER_BEARER_TOKEN_FILE` is configured, the Rust executor reads the
token from that `0600` file and sends it as a bearer credential. The included gateway
independently enforces chain ID, caller identity, executor destination, calldata
selector, gas/fee caps, zero value, and signed-transaction sender/field verification.

## Deployment signing

Contract deployment uses a separate, manually approved policy. The unsigned plan from
`scripts_prepare_deployment.py` represents contract creation with `"to": null` and
includes the exact init-code hashes and predicted addresses. A deployment-capable
external signer must:

- reject any init code whose hash is not in the reviewed plan;
- enforce Base chain ID `8453`, the reviewed nonce sequence, and zero transaction value;
- return the signed raw transaction without broadcasting it independently;
- refuse `setPaused(false)` during deployment;
- require a separate policy and signer authorization for `acceptOwnership()`.

Raw private keys must never be passed to Foundry, this repository, or the planner.

The included gateway exposes deployment signing separately at `/sign-deployment`.
Both `SIGNER_ALLOW_DEPLOYMENT` and `SIGNER_ALLOW_EXECUTION` default to `false`.
