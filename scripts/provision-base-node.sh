#!/usr/bin/env bash
#
# Provision a Base full node (base-reth-node + base consensus) on a fresh
# bare-metal host, sync it from a snapshot, and bring it up.
#
# Designed for a US-East box (colocate with the bot: sequencer proximity is
# worth more than node speed for arb). Mirrors the working local setup.
#
#   Required:
#     L1_ETH_RPC     Ethereum L1 execution RPC (Alchemy/QuickNode/your own)
#   Optional:
#     L1_BEACON      L1 beacon API      (default: publicnode)
#     DATA_DISKS     e.g. "/dev/nvme0n1 /dev/nvme1n1" -> DESTROYS these, RAID0
#     MOUNT          default /mnt/basenode
#     MANIFEST_URL   pin a snapshot manifest (default: discover latest)
#     BASE_TAG       default v1.2.0
#
#   Usage:
#     L1_ETH_RPC=https://... ./provision-base-node.sh --check
#     L1_ETH_RPC=https://... DATA_DISKS="/dev/nvme0n1 /dev/nvme1n1" \
#       ./provision-base-node.sh --format-disks
#     L1_ETH_RPC=https://... ./provision-base-node.sh
#
set -euo pipefail

MOUNT=${MOUNT:-/mnt/basenode}
BASE_TAG=${BASE_TAG:-v1.2.0}
BASE_REPO=${BASE_REPO:-https://github.com/base/base.git}
L1_BEACON=${L1_BEACON:-https://ethereum-beacon-api.publicnode.com}
NODE_DIR="$MOUNT/node"
DATA_DIR="$MOUNT/data"
FORMAT=0; CHECK_ONLY=0

for a in "$@"; do
  case "$a" in
    --format-disks) FORMAT=1 ;;
    --check)        CHECK_ONLY=1 ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- preflight
say "Preflight"
[ "$(id -u)" -eq 0 ] || die "run as root (disk + docker setup)"
[ -n "${L1_ETH_RPC:-}" ] || die "L1_ETH_RPC is required (an L1 execution RPC URL).
  A Base node derives L2 from L1 - it cannot sync without one.
  Do NOT reuse a key from another host's .env; mint a fresh one."

CORES=$(nproc); RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
echo "  cores=$CORES  ram=${RAM_GB}GB"
[ "$CORES" -ge 8 ]   || echo "  WARN: Base recommends 8+ cores (have $CORES)"
[ "$RAM_GB" -ge 30 ] || echo "  WARN: Base recommends 32GB+ RAM (have ${RAM_GB}GB)"

echo "  probing L1 RPC..."
RESP=$(curl -s --max-time 20 --retry 3 --retry-delay 2 --retry-all-errors \
  -X POST "$L1_ETH_RPC" -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}') || \
  die "cannot reach L1_ETH_RPC (network/TLS failure, not a chain mismatch).
  Check connectivity and the URL, then retry."
CHAINID=$(printf '%s' "$RESP" | sed -nE 's/.*"result":"([^"]*)".*/\1/p')
[ -n "$CHAINID" ] || die "L1_ETH_RPC reachable but returned no chainId.
  Response was: ${RESP:0:200}"
[ "$CHAINID" = "0x1" ] || die "L1_ETH_RPC is not Ethereum mainnet: chainId $CHAINID$(
  [ "$CHAINID" = "0x2105" ] && printf '%s' " -- that is Base itself.
  A Base node derives L2 state FROM L1; it needs an Ethereum mainnet RPC here.")"
echo "  L1 OK (chainId 0x1)"

echo "  probing L1 beacon..."
curl -sf --max-time 20 --retry 3 --retry-delay 2 --retry-all-errors \
  -o /dev/null "$L1_BEACON/eth/v1/node/version" \
  || die "L1_BEACON unreachable: $L1_BEACON"
echo "  beacon OK"

if [ "$CHECK_ONLY" = 1 ]; then say "--check passed; no changes made"; exit 0; fi

# ---------------------------------------------------------------- packages
say "Packages"
if ! have docker; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git jq mdadm >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker
fi
docker --version; docker compose version | head -1

# ---------------------------------------------------------------- disks
if [ "$FORMAT" = 1 ]; then
  [ -n "${DATA_DISKS:-}" ] || die "--format-disks needs DATA_DISKS=\"/dev/nvme0n1 ...\""
  say "DESTRUCTIVE: formatting $DATA_DISKS"
  for d in $DATA_DISKS; do [ -b "$d" ] || die "not a block device: $d"; done
  mountpoint -q "$MOUNT" && die "$MOUNT already mounted - refusing to reformat"
  lsblk -o NAME,SIZE,MOUNTPOINT $DATA_DISKS
  printf '\nType EXACTLY "destroy" to wipe the above: '
  read -r ok; [ "$ok" = "destroy" ] || die "aborted"

  N=$(echo "$DATA_DISKS" | wc -w)
  if [ "$N" -gt 1 ]; then
    # RAID0: Base recommends it, and a node is disposable - resync from snapshot.
    mdadm --create --verbose /dev/md0 --level=0 --raid-devices="$N" $DATA_DISKS --run
    TARGET=/dev/md0
    mdadm --detail --scan >> /etc/mdadm/mdadm.conf
    update-initramfs -u >/dev/null 2>&1 || true
  else
    TARGET=$DATA_DISKS
  fi
  # -m 0: no reserved blocks; this is a dedicated data volume, 5% would waste ~100GB
  mkfs.ext4 -F -m 0 -L basenode "$TARGET"
  mkdir -p "$MOUNT"
  UUID=$(blkid -s UUID -o value "$TARGET")
  grep -q "$UUID" /etc/fstab || \
    echo "UUID=$UUID $MOUNT ext4 defaults,noatime 0 2" >> /etc/fstab
  mount "$MOUNT"
fi

mountpoint -q "$MOUNT" || echo "WARN: $MOUNT is not a separate mount - using rootfs"
mkdir -p "$DATA_DIR"
AVAIL=$(df -BG --output=avail "$MOUNT" | tail -1 | tr -dc '0-9')
echo "  $MOUNT available: ${AVAIL}G"
[ "$AVAIL" -ge 1300 ] || die "need >=1300G free for the minimal snapshot
  (703GB archive + ~1085GB extracted; they overlap during extraction).
  Have ${AVAIL}G."

# ---------------------------------------------------------------- repo
say "base node repo @ $BASE_TAG"
if [ -d "$NODE_DIR/.git" ]; then
  git -C "$NODE_DIR" fetch --tags --depth 1 origin "$BASE_TAG"
  git -C "$NODE_DIR" checkout -q "$BASE_TAG"
else
  git clone --depth 1 --branch "$BASE_TAG" "$BASE_REPO" "$NODE_DIR"
fi
git -C "$NODE_DIR" log -1 --oneline

# ---------------------------------------------------------------- config
say "Config (fresh JWT - never copied between hosts)"
JWT=$(openssl rand -hex 32)
printf 'HOST_DATA_DIR=%s\n' "$DATA_DIR" > "$NODE_DIR/.env"

ENVF="$NODE_DIR/.env.mainnet"
cp "$ENVF" "$ENVF.orig" 2>/dev/null || true
set_kv(){ # key value file
  if grep -q "^$1=" "$3"; then
    python3 - "$1" "$2" "$3" <<'PY'
import sys
k,v,f=sys.argv[1],sys.argv[2],sys.argv[3]
lines=open(f).read().split('\n')
out=[(f"{k}={v}" if l.startswith(k+"=") else l) for l in lines]
open(f,'w').write('\n'.join(out))
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$3"
  fi
}
set_kv BASE_NODE_L1_ETH_RPC        "$L1_ETH_RPC" "$ENVF"
set_kv BASE_NODE_L1_BEACON         "$L1_BEACON"  "$ENVF"
set_kv BASE_NODE_L2_ENGINE_AUTH_RAW "$JWT"       "$ENVF"
set_kv HOST_DATA_DIR               "$DATA_DIR"   "$ENVF"
chmod 600 "$ENVF" "$NODE_DIR/.env"
echo "  wrote $ENVF (mode 600)"

# ---------------------------------------------------------------- build
say "Build execution image"
cd "$NODE_DIR"
docker compose build execution 2>&1 | tail -5
IMG=$(docker compose config --images 2>/dev/null | head -1)
IMG=${IMG:-node-execution}

# ---------------------------------------------------------------- snapshot
say "Snapshot download (the long step)"
# Default to discovering the latest manifest. Pinning only matters when a sync
# spans a manifest rotation; on a 1Gbit link this is ~2h, so latest is right.
# Set MANIFEST_URL to resume/reproduce a specific snapshot.
PIN=()
[ -n "${MANIFEST_URL:-}" ] && PIN=(--manifest-url "$MANIFEST_URL")
echo "  NOTE: state.tar.zst has NO piece-level resume. If this is interrupted,"
echo "        the whole ~705GB blob restarts. Run it to completion."
docker rm -f base-snapshot-dl >/dev/null 2>&1 || true
docker run --rm --name base-snapshot-dl \
  -v "$DATA_DIR":/data -w /app --entrypoint /app/base-reth-node \
  "$IMG" download --minimal -y --chain base --datadir /data "${PIN[@]}"

# ---------------------------------------------------------------- up
say "Starting node"
docker compose up -d
echo "  waiting for RPC on :8545 ..."
for i in $(seq 1 60); do
  BN=$(curl -s --max-time 5 -X POST http://localhost:8545 \
      -H 'content-type: application/json' \
      --data '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' \
      | sed -E 's/.*"result":"([^"]*)".*/\1/') || true
  case "$BN" in 0x*) echo "  RPC up, head=$((BN)) "; break ;; esac
  sleep 10
done

say "Done"
cat <<EOF
  RPC  : http://<host>:8545     (firewall this - do not expose publicly)
  WS   : ws://<host>:8546
  data : $DATA_DIR
  logs : cd $NODE_DIR && docker compose logs -f

  Point the bot at it:
    BASE_HTTP_RPC=http://127.0.0.1:8545
  The node still needs to catch up from the snapshot block to chain head;
  watch 'docker compose logs -f node' until it stops advancing rapidly.
EOF
