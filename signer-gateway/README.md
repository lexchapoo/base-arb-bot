# Local Clef signer gateway

This service is a loopback-only policy boundary between the Rust executor and
[Geth Clef](https://geth.ethereum.org/docs/tools/clef/introduction). It contains no
private-key import, storage, or signing code. Clef owns the encrypted key material
and returns an EIP-1559 signed transaction over a local Unix socket.

The gateway independently enforces:

- Base Mainnet chain ID `8453`, the configured sender, zero ETH value, and fee/gas caps;
- only `start(...)` or `startPacked(bytes)` calls to the configured executor;
- execution and deployment signing as separate, default-off capabilities;
- exact nonce, destination, calldata, and purpose from a SHA-256-bound deployment plan;
- field-for-field validation and sender recovery on every raw transaction returned by Clef;
- bearer authentication using a file with `0600` permissions.

Execution and reviewed deployment transactions use separate gas ceilings. The default
execution ceiling remains `3,000,000`; deployment permits up to `6,000,000` gas for
the reviewed executor constructor without increasing the live execution allowance.

## 1. Set up Clef outside the repository

Clef was removed from Geth beginning with Geth 1.17.4 and moved to the official
[`ethereum/clef`](https://github.com/ethereum/clef) standalone repository. Install a
pinned, reviewed build from that repository. Use dedicated directories outside this
repository for its config, encrypted keystore, IPC socket, and gateway token.

This workstation currently has a source build pinned to commit
`5924e70366fedd61b7637eb3f06e2aa0f0522185` installed at
`/home/shiesty/.local/bin/clef`. The active account was initialized using Clef's
secure default paths:

```bash
clef --configdir /home/shiesty/.clef init
clef --keystore /home/shiesty/.ethereum/keystore newaccount
```

Run both commands yourself in a private terminal. `clef init` displays a master seed;
store that recovery material offline and never paste it into chat, shell history, an
environment file, or this repository. Use strong, distinct passwords when prompted.

Do not put a raw private key, seed phrase, keystore password, or Clef masterseed in
this repository or its `.env`. If the configured address is currently controlled by
a browser wallet, the safer path is to create a dedicated Clef account, update
`EXTERNAL_SIGNER_ADDRESS` and `EXECUTOR_OWNER_ADDRESS`, and fund only the deployment
gas it needs.

Create a separate local bearer token without printing it:

```bash
install -d -m 700 /home/shiesty/.local/share/base-arb-signer/secrets
umask 077
openssl rand -hex 32 > /home/shiesty/.local/share/base-arb-signer/secrets/gateway-token
chmod 600 /home/shiesty/.local/share/base-arb-signer/secrets/gateway-token
```

Start Clef with Base's chain ID and a Unix socket. Keep its interactive console open
so each deployment transaction can be reviewed and approved manually:

```bash
clef \
  --keystore /home/shiesty/.ethereum/keystore \
  --configdir /home/shiesty/.clef \
  --chainid 8453 \
  --ipcpath /run/user/1000/base-arb-clef.ipc
```

Adjust `/run/user/1000` for the local user ID. Do not expose Clef over HTTP or enable
automatic Clef rules for the initial deployment.

## 2. Configure and run the gateway

`make setup` installs the gateway into the existing project virtual environment.
Copy `signer-gateway/.env.example` to an ignored local file, then set:

```env
SIGNER_GATEWAY_ADDRESS=0xYourClefAccount
EXECUTOR_ADDRESS=
CLEF_IPC_PATH=/run/user/1000/base-arb-clef.ipc/clef.ipc
SIGNER_GATEWAY_TOKEN_FILE=/home/shiesty/.local/share/base-arb-signer/secrets/gateway-token
SIGNER_ALLOW_EXECUTION=false
SIGNER_ALLOW_DEPLOYMENT=false
SIGNER_DEPLOYMENT_PLAN_PATH=
```

Start the gateway on loopback port `9000`:

```bash
set -a
source signer-gateway/.env
set +a
python/.venv/bin/python -m signer_gateway.app
```

The bot points at `http://127.0.0.1:9000/sign`; the gateway reaches Clef only through
the configured newline-delimited JSON-RPC Unix socket. Responses are bounded to 1 MiB
so large constructor responses fit without allowing unbounded reads. A bearer token
path—not the token itself—belongs in the
root `.env`:

```env
EXTERNAL_SIGNER_URL=http://127.0.0.1:9000/sign
EXTERNAL_SIGNER_BEARER_TOKEN_FILE=/home/shiesty/.local/share/base-arb-signer/secrets/gateway-token
```

## 3. Review and deploy while trading remains disabled

Keep these root settings throughout deployment:

```env
DRY_RUN=true
LIVE_TRADING=false
```

Build/test the contracts, query the Clef account's pending nonce, and create a new
private deployment-plan file:

```bash
make solidity-test
set -a
source .env
set +a
python scripts_prepare_deployment.py \
  --nonce <pending-clef-account-nonce> \
  --output /secure/base-arb/deployment-plan.json
```

Review the plan's bytecode hashes, addresses, allowlists, nonce sequence, and every
transaction. Then set only the gateway's deployment capability:

```env
SIGNER_ALLOW_DEPLOYMENT=true
SIGNER_ALLOW_EXECUTION=false
SIGNER_DEPLOYMENT_PLAN_PATH=/secure/base-arb/deployment-plan.json
```

Restart the gateway, preview the exact deployment request, and verify that it reports
`"broadcast": false`:

```bash
python/.venv/bin/python scripts_deploy_via_signer.py \
  --plan /secure/base-arb/deployment-plan.json
```

The following command spends real Base ETH and submits the reviewed transactions. It
still keeps trading disabled and requires approval for each transaction in Clef:

```bash
DEPLOYMENT_BROADCAST_ACK=I_UNDERSTAND_BASE_DEPLOYMENT \
python/.venv/bin/python scripts_deploy_via_signer.py \
  --plan /secure/base-arb/deployment-plan.json \
  --broadcast
```

After deployment, immediately set `SIGNER_ALLOW_DEPLOYMENT=false`, restart the
gateway, record the verified executor/adapter addresses, and leave
`SIGNER_ALLOW_EXECUTION=false`. Enabling funded execution is a later, explicit canary
decision after the required shadow-mode gates pass.
