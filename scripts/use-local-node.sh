#!/usr/bin/env bash
#
# Switch the bot's read endpoints to the local Base node, keeping the existing
# provider as failover / disagreement source.
#
# Refuses unless the node is actually caught up. RotatingProviders round-robins
# across the pool, so a stale node poisons a fraction of every read rather than
# failing loudly -- which is far worse than the rate limits it replaces.
#
#   ./scripts/use-local-node.sh            # switch (guarded)
#   ./scripts/use-local-node.sh --status   # just report sync state
#   ./scripts/use-local-node.sh --revert   # back to previous provider only
#
set -euo pipefail
cd "$(dirname "$0")/.."

NODE_HTTP=${NODE_HTTP:-http://172.17.0.1:8545}
NODE_WS=${NODE_WS:-ws://172.17.0.1:8546}
PUBLIC=${PUBLIC:-https://mainnet.base.org}
MAX_LAG=${MAX_LAG:-60}     # blocks; Base is ~2s/block so 60 ~= 2 minutes
ENVF=.env

die(){ printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }
say(){ printf '\033[1;36m%s\033[0m\n' "$*"; }

bn(){ curl -s --max-time 10 -X POST "$1" -H 'content-type: application/json' \
      --data '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' \
      | grep -oP '"result":"\K[^"]+' || true; }

check_sync(){
  local L R
  L=$(bn "$NODE_HTTP"); [ -n "$L" ] || die "local node not responding at $NODE_HTTP"
  R=$(bn "$PUBLIC");    [ -n "$R" ] || die "cannot reach $PUBLIC to compare"
  LOCAL=$((L)); REMOTE=$((R)); LAG=$((REMOTE-LOCAL))
  printf '  local  : %d\n  chain  : %d\n  lag    : %d blocks (threshold %d)\n' \
    "$LOCAL" "$REMOTE" "$LAG" "$MAX_LAG"
}

case "${1:-switch}" in
  --status)
    say "Sync status"; check_sync
    [ "$LAG" -le "$MAX_LAG" ] && echo "  -> CAUGHT UP, safe to switch" || echo "  -> still syncing, do NOT switch"
    exit 0 ;;
  --revert)
    B=$(ls -1t /mnt/basenode/env.backup.* 2>/dev/null | head -1) || true
    [ -n "${B:-}" ] || die "no backup found in /mnt/basenode/"
    cp "$ENVF" "$ENVF.before-revert"
    cp "$B" "$ENVF"
    say "reverted .env from $B"; exit 0 ;;
  switch) ;;
  *) die "unknown arg: $1" ;;
esac

say "Checking sync before switching"
check_sync
[ "$LAG" -le "$MAX_LAG" ] || die "node is $LAG blocks behind (max $MAX_LAG). Refusing to switch.
  A stale node in the rotation returns wrong prices on a fraction of reads
  instead of erroring. Wait for sync, then re-run."

# preserve whatever provider is currently primary, as failover
PREV=$(grep -E '^BASE_HTTP_RPC=' "$ENVF" | head -1 | cut -d= -f2- || true)
[ -n "$PREV" ] || die "could not read current BASE_HTTP_RPC from $ENVF"
case "$PREV" in "$NODE_HTTP") die "already pointed at the local node";; esac

cp "$ENVF" "/mnt/basenode/env.backup.$(date +%Y%m%d-%H%M%S)"
python3 - "$ENVF" "$NODE_HTTP" "$NODE_WS" "$PREV" <<'PY'
import sys,re
f,http,ws,prev=sys.argv[1:5]
lines=open(f).read().split('\n')
out=[]
seen=set()
for l in lines:
    if l.startswith('BASE_HTTP_RPC='):   out.append(f'BASE_HTTP_RPC={http}');  seen.add('h')
    elif l.startswith('BASE_WS_RPC='):   out.append(f'BASE_WS_RPC={ws}');      seen.add('w')
    elif l.startswith('BASE_HTTP_RPCS='):out.append(f'BASE_HTTP_RPCS={prev}'); seen.add('s')
    else: out.append(l)
if 'h' not in seen: out.append(f'BASE_HTTP_RPC={http}')
if 'w' not in seen: out.append(f'BASE_WS_RPC={ws}')
if 's' not in seen: out.append(f'BASE_HTTP_RPCS={prev}')
open(f,'w').write('\n'.join(out))
PY

say "Switched:"
echo "  BASE_HTTP_RPC  = $NODE_HTTP        (primary, unlimited, sub-ms)"
echo "  BASE_WS_RPC    = $NODE_WS"
echo "  BASE_HTTP_RPCS = <previous provider>   (failover + provider disagreement)"
echo
echo "Apply with: docker compose up -d --force-recreate python-api rust-executor"
