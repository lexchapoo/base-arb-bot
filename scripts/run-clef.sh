#!/usr/bin/env bash
# Start Clef for this project, with every path pinned.
#
#   scripts/run-clef.sh
#
# Exists for the same reason as run-signer-gateway.sh: the invocation carries flags whose
# defaults are wrong here, and getting one of them wrong fails silently rather than loudly.
#
# --auditlog is the reason this script was written. Its default is the RELATIVE path
# "audit.log", so Clef writes the record of every signature it serves into whatever directory
# the operator happened to launch it from. That is how the two Base mainnet upgrade signatures
# came to be absent from the audit.log this repository was tracking: Clef was started from
# $HOME that day, so they went to ~/audit.log, while the committed file kept only the
# signatures from sessions launched at the repository root. A partial audit log that reads as
# authoritative is worse than none, so the path is fixed here and never inherited from cwd.
#
# Runs in the foreground and must stay there: Clef prompts on this terminal for the master
# password at startup and for approval of every individual signature.
set -euo pipefail

CLEF_CONFIGDIR="${CLEF_CONFIGDIR:-$HOME/.clef}"
CLEF_KEYSTORE="${CLEF_KEYSTORE:-$HOME/.ethereum/keystore}"
CLEF_CHAINID="${CLEF_CHAINID:-8453}"
# Kept inside the 0700 configdir rather than beside the code: it names accounts and carries
# raw signed transactions, including ones never broadcast, and this repository is public.
CLEF_AUDITLOG="${CLEF_AUDITLOG:-$CLEF_CONFIGDIR/audit.log}"
# Pinned explicitly even though it matches Clef's default, because run-signer-gateway.sh
# resolves the same path independently and the two must not drift apart.
CLEF_IPCPATH="${CLEF_IPCPATH:-$CLEF_CONFIGDIR/clef.ipc}"

if ! command -v clef >/dev/null 2>&1; then
    echo "clef not found on PATH" >&2
    exit 2
fi

for required in "$CLEF_CONFIGDIR" "$CLEF_KEYSTORE"; do
    if [[ ! -d "$required" ]]; then
        echo "directory does not exist: $required" >&2
        echo "run 'clef --configdir $CLEF_CONFIGDIR init' first; see signer-gateway/README.md" >&2
        exit 2
    fi
done

# An already-running Clef holds this socket. Starting a second one unlinks the socket from
# under the first, which keeps running and keeps prompting while the gateway talks to nobody.
if [[ -S "$CLEF_IPCPATH" ]] && python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
try:
    s.connect('$CLEF_IPCPATH')
except OSError:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; then
    echo "Clef is already running and listening on $CLEF_IPCPATH" >&2
    echo "Use that instance, or stop it first. Starting a second would orphan the first." >&2
    exit 2
fi

# Created ahead of Clef so the mode is ours, not the default umask's. Clef appends.
install -d -m 700 "$CLEF_CONFIGDIR"
[[ -e "$CLEF_AUDITLOG" ]] || install -m 600 /dev/null "$CLEF_AUDITLOG"

echo "configdir : $CLEF_CONFIGDIR"
echo "keystore  : $CLEF_KEYSTORE"
echo "chain id  : $CLEF_CHAINID"
echo "ipc path  : $CLEF_IPCPATH"
echo "audit log : $CLEF_AUDITLOG"
echo
echo "Clef prompts HERE for the master password and for every signature. Leave it running."
echo

exec clef \
    --configdir "$CLEF_CONFIGDIR" \
    --keystore "$CLEF_KEYSTORE" \
    --chainid "$CLEF_CHAINID" \
    --ipcpath "$CLEF_IPCPATH" \
    --auditlog "$CLEF_AUDITLOG"
