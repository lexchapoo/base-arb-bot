#!/usr/bin/env bash
# Start the local Clef signer gateway for a reviewed deployment plan.
#
#   scripts/run-signer-gateway.sh plans/upgrade-phase1-nonce18.json
#
# Exists because the gateway needs environment the repo's .env does not carry: the package
# is not installed into python/.venv (it is a separate project under signer-gateway/), and
# it reads SIGNER_GATEWAY_* where .env defines the client-side EXTERNAL_SIGNER_* names. Every
# one of those was a separate failed start before this script existed.
#
# Runs in the foreground. Clef must already be running in its own terminal, and will prompt
# there for approval of each signature.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PLAN="${1:-${SIGNER_DEPLOYMENT_PLAN_PATH:-}}"
if [[ -z "$PLAN" ]]; then
    echo "usage: $0 <path-to-reviewed-plan.json>" >&2
    exit 2
fi
PLAN="$(cd "$(dirname "$PLAN")" && pwd)/$(basename "$PLAN")"
if [[ ! -f "$PLAN" ]]; then
    echo "plan not found: $PLAN" >&2
    exit 2
fi

if [[ ! -f .env ]]; then
    echo ".env not found in $ROOT" >&2
    exit 2
fi
set -a; . ./.env; set +a

# The gateway is a separate distribution and is deliberately not installed into the bot's
# virtualenv; all six of its dependencies are already satisfied there, so a path is enough.
export PYTHONPATH="$ROOT/signer-gateway/src${PYTHONPATH:+:$PYTHONPATH}"

# .env carries the client-side names; the gateway reads its own.
export SIGNER_GATEWAY_ADDRESS="${SIGNER_GATEWAY_ADDRESS:-${EXTERNAL_SIGNER_ADDRESS:-}}"
export SIGNER_GATEWAY_TOKEN_FILE="${SIGNER_GATEWAY_TOKEN_FILE:-${EXTERNAL_SIGNER_BEARER_TOKEN_FILE:-}}"
export CLEF_IPC_PATH="${CLEF_IPC_PATH:-$HOME/.clef/clef.ipc}"
export SIGNER_DEPLOYMENT_PLAN_PATH="$PLAN"

# Deployment only. Execution signing is a separate, later, explicit decision and must not be
# switched on as a side effect of deploying.
export SIGNER_ALLOW_DEPLOYMENT=true
export SIGNER_ALLOW_EXECUTION=false

# The default ceiling of 6,000,000 leaves ~11% headroom over the current implementation's
# ~5.37M estimate, so a small drift would have the gateway refuse to sign rather than the
# chain refuse the transaction. Override only if the caller has not already.
export SIGNER_MAX_DEPLOYMENT_GAS="${SIGNER_MAX_DEPLOYMENT_GAS:-7000000}"

for required in SIGNER_GATEWAY_ADDRESS SIGNER_GATEWAY_TOKEN_FILE; do
    if [[ -z "${!required:-}" ]]; then
        echo "$required is empty; set it, or its EXTERNAL_SIGNER_* equivalent, in .env" >&2
        exit 2
    fi
done

# Checked here to fail with one clear line instead of a lifespan traceback after uvicorn has
# started. `-S` alone is not enough: Clef removes this socket on a *clean* exit, but a crashed
# or killed Clef leaves the file behind, and then the existence test passes against a socket
# with nothing listening. That buys a gateway that starts, prints a plan, and reports itself
# ready -- and only fails once the operator has staged everything and a signature is actually
# requested, as a Clef connection error. So connect for real. (Clef itself unlinks a stale
# socket on start, so a stale one needs no cleanup here -- just a running Clef.)
clef_socket_state() {
    if [[ ! -S "$CLEF_IPC_PATH" ]]; then
        echo missing
        return
    fi
    if [[ ! -x python/.venv/bin/python ]]; then
        echo unprobed
        return
    fi
    CLEF_IPC_PATH="$CLEF_IPC_PATH" python/.venv/bin/python - <<'PY'
import os
import socket

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    sock.connect(os.environ["CLEF_IPC_PATH"])
except OSError:
    print("stale")
else:
    print("live")
finally:
    sock.close()
PY
}

CLEF_STATE="$(clef_socket_state)"
if [[ "$CLEF_STATE" != "live" ]]; then
    case "$CLEF_STATE" in
        missing)
            echo "Clef IPC socket not found: $CLEF_IPC_PATH" >&2
            echo >&2
            echo "Clef is not running (it removes this socket on a clean exit)." >&2
            ;;
        stale)
            echo "Clef IPC socket is stale: $CLEF_IPC_PATH" >&2
            echo >&2
            echo "The file exists but nothing is listening, so Clef exited uncleanly. Starting" >&2
            echo "Clef replaces the socket; it does not need to be removed by hand." >&2
            ;;
        *)
            echo "Could not probe the Clef IPC socket: $CLEF_IPC_PATH" >&2
            echo >&2
            echo "python/.venv/bin/python is missing, so socket liveness went unchecked -- and" >&2
            echo "the gateway needs that interpreter to start anyway. Create the virtualenv." >&2
            ;;
    esac
    cat >&2 <<EOF

Start Clef in its own terminal and leave it there -- it prompts for approval of every
signature:

  clef --configdir ~/.clef --keystore ~/.ethereum/keystore --chainid 8453

--keystore matters: without it Clef may start with no account for the deployer, which
only surfaces as a failure at signing time.
EOF
    exit 2
fi

echo "gateway   : 127.0.0.1:${SIGNER_GATEWAY_PORT:-9000}"
echo "signer    : $SIGNER_GATEWAY_ADDRESS"
echo "clef ipc  : $CLEF_IPC_PATH"
echo "plan      : $SIGNER_DEPLOYMENT_PLAN_PATH"
echo "plan sha  : $(sha256sum "$SIGNER_DEPLOYMENT_PLAN_PATH" | cut -d' ' -f1)"
echo "deployment signing ENABLED; execution signing disabled"
echo

exec python/.venv/bin/python -m signer_gateway.app
