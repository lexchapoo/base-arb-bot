#!/usr/bin/env python3
"""Rank discovered pools by real liquidity and register only the meaningful ones.

Full discovery finds ~28.6k Aerodrome pools, but the route engine enumerates
cycles synchronously on the event loop, and hub tokens (WETH/USDC) connect
thousands of pools each. Feeding it the whole set pegs one core and starves the
API -- 0 evaluations completed in 10 minutes when we tried.

Most of those pools are dead. This reads reserves via batched Multicall3 against
the local node (28k eth_calls: free and fast locally, prohibitive on a metered
provider) and keeps only pools whose hub-token side clears a floor.

  ./scripts/curate_pools.py --dry-run          # rank + report, register nothing
  ./scripts/curate_pools.py --top 400          # register the best 400
  ./scripts/curate_pools.py --min-weth 2 --min-usdc 5000
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from dataclasses import dataclass

import asyncpg
import httpx
from web3 import AsyncWeb3
from arb_bot.multicall import MulticallClient

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGG3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])
GET_RESERVES = "0x0902f1ac"  # getReserves()

# Base hub tokens: (symbol, decimals, default floor in whole units)
HUBS: dict[str, tuple[str, int, float]] = {
    "0x4200000000000000000000000000000000000006": ("WETH",  18, 1.0),
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC",   6, 2500.0),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC",  6, 2500.0),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI",   18, 2500.0),
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": ("cbBTC",  8, 0.03),
}


@dataclass
class Pool:
    address: str; token0: str; token1: str
    venue: str; pool_type: str; fee: int | None; stable: bool | None
    hub: str | None = None; hub_side: int = 0; hub_amount: float = 0.0


async def fetch_reserves(mc: MulticallClient, pools: list[Pool], batch: int) -> None:
    """Read getReserves() for every pool via the codebase's own Multicall3 client.

    allowFailure is on, so dead/self-destructed pools return success=False and
    are simply left at 0 liquidity rather than aborting the batch.
    """
    total = len(pools)
    data = bytes.fromhex(GET_RESERVES[2:])
    for s in range(0, total, batch):
        chunk = pools[s:s+batch]
        try:
            rets = await mc.aggregate3(
                [(p.address, data) for p in chunk], block_identifier="latest")
        except Exception as e:
            print(f"  batch {s}-{s+len(chunk)} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for p, (ok, r) in zip(chunk, rets):
            if not ok or len(r) < 64:
                continue
            r0 = int.from_bytes(r[0:32], "big"); r1 = int.from_bytes(r[32:64], "big")
            raw = r0 if p.hub_side == 0 else r1
            p.hub_amount = raw / (10 ** HUBS[p.hub][1])
        print(f"  reserves {min(s+batch,total)}/{total}", end="\r", flush=True)
    print()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--api", default=os.environ.get("CURATE_API", "http://127.0.0.1:8080"))
    ap.add_argument("--pg",  default=os.environ.get("CURATE_PG", "postgresql://arb:arb@127.0.0.1:5432/arb"))
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-weth", type=float, default=None)
    ap.add_argument("--min-usdc", type=float, default=None)
    a = ap.parse_args()

    floors = {addr: (sym, dec, flr) for addr, (sym, dec, flr) in HUBS.items()}
    if a.min_weth is not None:
        for k, (s, d, f) in floors.items():
            if s == "WETH": floors[k] = (s, d, a.min_weth)
    if a.min_usdc is not None:
        for k, (s, d, f) in floors.items():
            if s in ("USDC", "USDbC", "DAI"): floors[k] = (s, d, a.min_usdc)

    conn = await asyncpg.connect(a.pg)
    rows = await conn.fetch(
        "select address,token0,token1,venue,pool_type,fee,stable from verified_pools")
    await conn.close()
    print(f"loaded {len(rows):,} pools from verified_pools")

    pools: list[Pool] = []
    for r in rows:
        p = Pool(r["address"], r["token0"].lower(), r["token1"].lower(),
                 r["venue"], r["pool_type"], r["fee"], r["stable"])
        if p.token0 in HUBS:   p.hub, p.hub_side = p.token0, 0
        elif p.token1 in HUBS: p.hub, p.hub_side = p.token1, 1
        else: continue
        pools.append(p)
    print(f"  {len(pools):,} pair a hub token ({len(rows)-len(pools):,} skipped: no hub side)")

    mc = MulticallClient(a.rpc, attempt_timeout_seconds=30.0)
    print(f"reading reserves via Multicall3 (batch={a.batch}) against {a.rpc}")
    await fetch_reserves(mc, pools, a.batch)

    kept = [p for p in pools if p.hub_amount >= floors[p.hub][2]]
    kept.sort(key=lambda p: (floors[p.hub][0], -p.hub_amount))
    kept.sort(key=lambda p: -p.hub_amount)

    print(f"\n{len(kept):,} pools clear the liquidity floor:")
    by_hub: dict[str, int] = {}
    for p in kept: by_hub[floors[p.hub][0]] = by_hub.get(floors[p.hub][0], 0)+1
    for sym, n in sorted(by_hub.items(), key=lambda x: -x[1]):
        flr = next(f for (s, d, f) in floors.values() if s == sym)
        print(f"    {sym:6} {n:5,}  (floor {flr:g})")

    selected = kept[:a.top]
    print(f"\ntop {len(selected)} by hub-side reserve:")
    for p in selected[:10]:
        print(f"    {p.address}  {floors[p.hub][0]:6} {p.hub_amount:,.2f}")
    if len(selected) > 10: print(f"    ... {len(selected)-10} more")

    if a.dry_run:
        print("\n--dry-run: nothing registered"); return 0

    print(f"\nregistering {len(selected)} pools at {a.api}/pools/register")
    ok = fail = 0
    async with httpx.AsyncClient(timeout=30) as c:
        for p in selected:
            body = {"address": p.address, "token0": p.token0, "token1": p.token1,
                    "venue": p.venue, "pool_type": p.pool_type}
            if p.fee is not None: body["fee"] = p.fee
            if p.stable is not None: body["stable"] = p.stable
            try:
                r = await c.post(f"{a.api}/pools/register", json=body)
                ok += 1 if r.status_code < 300 else 0
                fail += 0 if r.status_code < 300 else 1
            except Exception:
                fail += 1
    print(f"  registered={ok} failed={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
