#!/usr/bin/env python3
"""One-time Uniswap V3 pool backfill against an archive RPC.

Why this exists: the local Base node was restored from the `--minimal` snapshot,
which carries Receipts for the last 64 blocks only. Historical eth_getLogs
therefore fails with `pruned history unavailable` (code 4444), and Uniswap V3
discovery -- which scans factory PoolCreated logs -- can never complete against
it. Aerodrome discovery is unaffected because it paginates allPools(i) with
eth_call rather than reading logs.

The consequence was stark: every pool in verified_pools was aerodrome/xyk, so
2-hop cycles were Aerodrome->Aerodrome on one curve and 100% of route
evaluations returned non_positive_gross_profit. Cross-venue arbitrage needs
more than one venue.

This runs the same BasePoolDiscovery.discover_uniswap() the service uses, but
pointed at an archive endpoint, writing through the same PostgresPoolRegistry.
It deliberately does NOT go through the API: enabling discovery there also runs
restore(), which reloads all ~28.6k pools into the graph and pegs the event
loop. Backfill here, then re-run scripts/curate_pools.py to register the
liquid subset.

  RPC=https://... ./scripts/backfill_uniswap_pools.py --dry-run
  RPC=https://... ./scripts/backfill_uniswap_pools.py --from-block 1371680
"""
from __future__ import annotations
import argparse, asyncio, os, sys, time

from arb_bot.config import settings
from arb_bot.event_graph import LivePoolGraph
from arb_bot.pool_discovery import BasePoolDiscovery, DiscoveryStats
from arb_bot.db.pool_registry import PostgresPoolRegistry
from web3 import AsyncWeb3, AsyncHTTPProvider

# Uniswap V3 factory deployment on Base. Scanning from 0 wastes ~68 chunks on
# blocks that predate the factory.
UNISWAP_V3_DEPLOY_BLOCK = 1_371_680


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("RPC", ""),
                    help="archive RPC (must serve historical eth_getLogs)")
    ap.add_argument("--from-block", type=int, default=UNISWAP_V3_DEPLOY_BLOCK)
    ap.add_argument("--chunk", type=int, default=settings.pool_discovery_log_chunk_blocks)
    ap.add_argument("--call-rpc", default=os.environ.get("CALL_RPC", "http://172.17.0.1:8545"),
                    help="endpoint for verification eth_calls (local node); logs use --rpc")
    ap.add_argument("--max-call-lag", type=int,
                    default=settings.pool_discovery_max_call_lag_blocks,
                    help="max blocks --call-rpc may trail --rpc before verification is "
                         "treated as untrustworthy")
    ap.add_argument("--max-stalled-passes", type=int, default=25,
                    help="give up after this many consecutive passes that do not advance "
                         "the cursor (exits non-zero: the scan is incomplete)")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify the RPC can serve historical logs, then stop")
    a = ap.parse_args()

    rpc = a.rpc or settings.base_http_rpc
    if not rpc:
        print("no RPC given (--rpc or $RPC)", file=sys.stderr); return 2
    host = rpc.split("/v2/")[0] if "/v2/" in rpc else rpc
    print(f"rpc      : {host}/...")
    print(f"factory  : {settings.uniswap_v3_factory}")
    print(f"from     : {a.from_block:,}  chunk {a.chunk:,}")
    print(f"calls    : {a.call_rpc}  (verification eth_calls)")

    w3 = AsyncWeb3(AsyncHTTPProvider(rpc, request_kwargs={"timeout": 60}))
    latest = await w3.eth.block_number
    print(f"head     : {latest:,}  ({(latest-a.from_block)//a.chunk:,} chunks to scan)")

    # Fail fast if this endpoint cannot serve old logs -- that is the whole point.
    probe_from = a.from_block
    try:
        await w3.eth.get_logs({
            "fromBlock": probe_from, "toBlock": probe_from + 100,
            "address": AsyncWeb3.to_checksum_address(settings.uniswap_v3_factory),
        })
        print("probe    : historical getLogs OK")
    except Exception as e:
        print(f"probe    : FAILED -- {type(e).__name__}: {e}", file=sys.stderr)
        print("  this endpoint cannot serve historical logs; use an archive provider",
              file=sys.stderr)
        return 3

    if a.dry_run:
        print("\n--dry-run: endpoint is capable; nothing scanned")
        return 0

    store = PostgresPoolRegistry()
    graph = LivePoolGraph()
    disc = BasePoolDiscovery(
        rpc, graph,
        aerodrome_factory="",                      # aerodrome already complete
        uniswap_v3_factory=settings.uniswap_v3_factory,
        slipstream_factory=settings.aerodrome_slipstream_factory,
        uniswap_from_block=a.from_block,
        log_chunk_blocks=a.chunk,
        concurrency=settings.pool_discovery_concurrency,
        call_rpc_url=a.call_rpc,
        max_call_endpoint_lag_blocks=a.max_call_lag,
        token_allowlist=[x for x in settings.pool_discovery_token_allowlist.split(",") if x.strip()],
        store=store,
    )
    # A wrong-chain or lagging --call-rpc makes every verification say "no code there",
    # which discovery cannot distinguish from "pool does not exist". Prove the two
    # endpoints agree before writing a single cursor.
    try:
        endpoints = await disc.verify_endpoint_consistency(force=True)
    except Exception as e:
        print(f"endpoints: FAILED -- {e}", file=sys.stderr)
        return 4
    if endpoints:
        print(f"endpoints: log_head={endpoints['log_head']:,} "
              f"call_head={endpoints['call_head']:,} lag={endpoints['call_endpoint_lag']}")

    # restore() seeds cursors from discovery_checkpoints so a killed run resumes.
    restored = await disc.restore()
    print(f"restored : {restored}")

    stats = DiscoveryStats()
    t0 = time.time()
    # discover_uniswap() returns early on the first chunk with a transient failure --
    # by design, since the service drives it from a repeating refresh loop. For a
    # one-time backfill we drive that loop ourselves until the cursor reaches head.
    stuck = 0
    # Set to the reason the scan did not reach `latest`. Anything other than None means
    # the backfill is incomplete and the caller must not treat it as done -- an exit code
    # of 0 here previously told CI (and the operator) that a cursor stranded thousands of
    # blocks behind head was a finished backfill.
    incomplete: str | None = None
    try:
        while disc._uniswap_cursor <= latest:
            before = disc._uniswap_cursor
            await disc.discover_uniswap(stats, latest)
            after = disc._uniswap_cursor
            # A code-absent pool holds the cursor on purpose and is never skipped, so
            # waiting out `--max-stalled-passes` would only delay the same verdict while
            # reporting it as a generic stall. Name the real cause immediately.
            blocked = disc.code_missing_blockers().get("uniswap-v3")
            if blocked:
                incomplete = f"blocked on an unverifiable pool -- {blocked}"
                print(f"\n{incomplete}", file=sys.stderr)
                print("  the --call-rpc endpoint cannot see a pool the factory announced; "
                      "point it at a synced Base node and re-run", file=sys.stderr)
                break
            if after <= before:
                stuck += 1
                if stuck >= a.max_stalled_passes:
                    incomplete = (f"cursor stuck at {after:,} after {stuck} passes "
                                  f"({latest-after:,} blocks short of head)")
                    print(f"\n{incomplete}; giving up", file=sys.stderr)
                    break
                await asyncio.sleep(2.0)
            else:
                stuck = 0
                pct = 100.0 * (after - a.from_block) / max(1, latest - a.from_block)
                print(f"  cursor {after:,}/{latest:,} ({pct:5.1f}%)  "
                      f"seen={stats.uniswap_seen} registered={stats.registered}",
                      end="\r", flush=True)
        print()
    except KeyboardInterrupt:
        incomplete = "interrupted by SIGINT"
        print("\ninterrupted -- cursor is checkpointed, re-run to resume")
    except Exception as e:
        print(f"\nscan stopped: {type(e).__name__}: {e}", file=sys.stderr)
        print("cursor is checkpointed; re-run to resume", file=sys.stderr)
        print(f"partial stats: {stats.as_dict()}", file=sys.stderr)
        return 1

    # The loop can also exit cleanly with the cursor short of head (discover_uniswap
    # returns early on a chunk it could not verify). Check the cursor itself rather than
    # trusting that falling out of the loop means completion.
    if incomplete is None and disc._uniswap_cursor <= latest:
        incomplete = (f"cursor stopped at {disc._uniswap_cursor:,}, "
                      f"{latest - disc._uniswap_cursor + 1:,} blocks short of head {latest:,}")

    print(f"\nelapsed  : {time.time()-t0:,.0f}s")
    print(f"stats    : {stats.as_dict()}")
    if incomplete is not None:
        print(f"\nINCOMPLETE: {incomplete}", file=sys.stderr)
        print("cursor is checkpointed; re-run to resume before curating", file=sys.stderr)
        return 1
    print("done -- cursor reached head")
    print("\nnext: ./scripts/curate_pools.py   (re-rank now that a second venue exists)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
