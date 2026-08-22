# External Signer Contract

The Rust executor never accepts raw private keys.

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

The signer must independently enforce chain ID, caller policy, executor destination, and any key-management policy appropriate to the deployment.
