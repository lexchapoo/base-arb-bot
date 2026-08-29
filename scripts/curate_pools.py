#!/usr/bin/env python3
"""Rank discovered pools by real liquidity and register only the meaningful ones.

Full discovery finds ~28.6k Aerodrome pools, but the route engine enumerates
cycles synchronously on the event loop, and hub tokens (WETH/USDC) connect
thousands of pools each. Feeding it the whole set pegs one core and starves the
API -- 0 evaluations completed in 10 minutes when we tried.

Most of those pools are dead. This reads reserves via batched Multicall3 against
the local node (28k eth_calls: free and fast locally, prohibitive on a metered
provider) and keeps only pools whose hub-token side clears a floor.

Selection is a *set*, not an accumulation: the chosen pools are pushed to
/pools/select, which replaces the router's whole pool set and persists it, so
re-running with a tighter floor actually drops what no longer qualifies.

  ./scripts/curate_pools.py --dry-run          # rank + report, register nothing
  ./scripts/curate_pools.py --top 400          # register the best 400
  ./scripts/curate_pools.py --min-weth 2 --min-usdc 5000
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import asyncpg
import httpx
from web3 import AsyncWeb3
from arb_bot.event_graph import LivePoolGraph, PoolMetadata, canonical_venue
from arb_bot.multicall import MulticallClient

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGG3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])
BALANCE_OF = "0x70a08231"  # balanceOf(address)

# Base hub tokens: (symbol, decimals, default floor in whole units, USD reference price).
#
# The USD reference exists purely to make the ranking comparable. Without it the sort key
# was a raw token amount, so 2,500 USDC outranked 1 WETH and the "top N by liquidity"
# selection was really "top N by whichever hub has the largest unit count" -- stablecoin
# pools crowded out every WETH and cbBTC pool regardless of actual depth.
#
# These are coarse ranking scalars and never enter a quote or profit calculation. They are
# still Decimal from literal strings rather than float: a binary-float 0.03 or 2500.0 is
# not the number it prints as, and a floor comparison that is off in the seventeenth digit
# is a pool included or excluded for no reason anyone can reconstruct later. The volatile
# ones drift -- --weth-usd / --cbbtc-usd override them, and the values actually used are
# printed with the selection.
HUBS: dict[str, tuple[str, int, Decimal, Decimal]] = {
    "0x4200000000000000000000000000000000000006": ("WETH",  18, Decimal("1"),    Decimal("2500")),
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC",   6, Decimal("2500"), Decimal("1")),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC",  6, Decimal("2500"), Decimal("1")),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI",   18, Decimal("2500"), Decimal("1")),
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": ("cbBTC",  8, Decimal("0.03"), Decimal("85000")),
}


@dataclass
class Pool:
    address: str; token0: str; token1: str
    venue: str; pool_type: str; fee: int | None; stable: bool | None
    tick_spacing: int | None = None
    hub: str | None = None
    # Exact on-chain balance. Kept as the integer the node returned and converted with
    # Decimal at the point of use, so neither the floor test nor the ranking depends on
    # binary floating point.
    hub_raw: int = 0
    # False until a balance was actually read. Distinguishes "this pool holds nothing"
    # from "nobody told us what this pool holds" -- both used to score 0 and be dropped.
    resolved: bool = False

    @property
    def hub_amount(self) -> Decimal:
        """Hub-token balance in whole units."""
        return Decimal(self.hub_raw) / (Decimal(10) ** HUBS[self.hub][1])


def _decimal(raw: str) -> Decimal:
    """Decimal, not float: these feed the floor comparison and the ranking key directly."""
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a decimal number: {raw!r}") from None


def cross_venue_cycles(selected: list[Pool], max_hops: int) -> list[tuple[str, ...]]:
    """Enumerate cycles in the selection that actually span more than one venue.

    Counting distinct venues is not the same test. Two venues holding unrelated token
    pairs produce a selection with a healthy-looking venue histogram and not one
    executable route, which is exactly the outcome curation exists to prevent. This runs
    the router's own enumeration (LivePoolGraph.cycles_for_affected -- the same code that
    will later be asked for routes) over the selected set, so a pass here means the router
    will find at least one cross-venue cycle, not that it might.
    """
    graph = LivePoolGraph()
    for p in selected:
        graph.register(PoolMetadata.create(
            address=p.address, token0=p.token0, token1=p.token1,
            venue=p.venue, pool_type=p.pool_type, fee=p.fee,
            stable=p.stable, tick_spacing=p.tick_spacing,
        ))
    found: list[tuple[str, ...]] = []
    for cycle in graph.cycles_for_affected(graph.addresses(), max_hops=max_hops):
        venues = {canonical_venue(pool.venue) for pool in cycle.pools}
        if len(venues) > 1:
            found.append(tuple(pool.address for pool in cycle.pools))
    return found


def _balance_of_calldata(holder: str) -> bytes:
    """balanceOf(address) for `holder`, called against the token contract."""
    return bytes.fromhex(BALANCE_OF[2:]) + int(holder, 16).to_bytes(32, "big")


async def fetch_hub_balances(mc: MulticallClient, pools: list[Pool], batch: int) -> list[Pool]:
    """Read each pool's hub-token balance via the codebase's Multicall3 client.

    Deliberately balanceOf(pool) on the token rather than getReserves() on the pool:
    getReserves() only exists on xyk pairs, so Uniswap V3 / Slipstream pools would
    revert and be scored as zero liquidity. Token balance is venue-agnostic.

    Caveat for concentrated liquidity: balance counts tokens that may sit outside the
    active tick range, so it overstates immediately tradeable depth for V3 relative to
    an xyk pool holding the same amount. It is a ranking heuristic, not a quote.

    Returns the pools whose balance could not be read. A failed batch or a reverted
    subcall used to be indistinguishable from an empty pool: both scored zero and fell
    below the floor, so an RPC hiccup silently excluded good pools from the selection
    while the run still reported success.
    """
    total = len(pools)
    for s in range(0, total, batch):
        chunk = pools[s:s+batch]
        try:
            rets = await mc.aggregate3(
                [(p.hub, _balance_of_calldata(p.address)) for p in chunk],
                block_identifier="latest")
        except Exception as e:
            print(f"  batch {s}-{s+len(chunk)} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for p, (ok, r) in zip(chunk, rets):
            if not ok or len(r) < 32:
                continue
            p.hub_raw = int.from_bytes(r[0:32], "big")
            p.resolved = True
        print(f"  balances {min(s+batch,total)}/{total}", end="\r", flush=True)
    print()
    return [p for p in pools if not p.resolved]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--api", default=os.environ.get("CURATE_API", "http://127.0.0.1:8080"))
    ap.add_argument("--pg",  default=os.environ.get("CURATE_PG", "postgresql://arb:arb@127.0.0.1:5432/arb"))
    ap.add_argument("--api-token", default=os.environ.get("OPERATOR_API_TOKEN", ""),
                    help="operator token for /pools/select (default $OPERATOR_API_TOKEN)")
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    # _decimal, not Decimal: argparse turns ValueError/TypeError from a converter into a
    # clean "error: argument --min-weth: ..." message, but Decimal("abc") raises
    # InvalidOperation, which derives from ArithmeticError and escapes as a raw traceback.
    ap.add_argument("--min-weth", type=_decimal, default=None)
    ap.add_argument("--min-usdc", type=_decimal, default=None)
    ap.add_argument("--weth-usd", type=_decimal, default=None,
                    help="WETH reference price used to rank across hub tokens")
    ap.add_argument("--cbbtc-usd", type=_decimal, default=None,
                    help="cbBTC reference price used to rank across hub tokens")
    ap.add_argument("--validate-hops", type=int, default=2, choices=(2, 3),
                    help="cycle length used to prove the selection is arbitrageable "
                         "(2 = same pair on two venues; 3 is far more expensive to enumerate)")
    ap.add_argument("--allow-no-cross-venue-cycle", "--allow-single-venue",
                    dest="allow_no_cross_venue_cycle", action="store_true",
                    help="register a selection containing no executable cross-venue cycle "
                         "(the router cannot arbitrage it, so this is refused by default)")
    ap.add_argument("--allow-partial-balances", action="store_true",
                    help="curate from a partial balance read (pools whose balance could "
                         "not be read are excluded and counted, instead of failing the run)")
    a = ap.parse_args()

    floors = dict(HUBS)
    if a.min_weth is not None:
        for k, (sym, d, f, usd) in floors.items():
            if sym == "WETH": floors[k] = (sym, d, a.min_weth, usd)
    if a.min_usdc is not None:
        for k, (sym, d, f, usd) in floors.items():
            if sym in ("USDC", "USDbC", "DAI"): floors[k] = (sym, d, a.min_usdc, usd)
    if a.weth_usd is not None:
        for k, (sym, d, f, usd) in floors.items():
            if sym == "WETH": floors[k] = (sym, d, f, a.weth_usd)
    if a.cbbtc_usd is not None:
        for k, (sym, d, f, usd) in floors.items():
            if sym == "cbBTC": floors[k] = (sym, d, f, a.cbbtc_usd)

    def hub_usd(p: Pool) -> Decimal:
        """Rank key: hub-side depth in USD, so hubs are actually comparable."""
        return p.hub_amount * floors[p.hub][3]

    conn = await asyncpg.connect(a.pg)
    rows = await conn.fetch(
        "select address,token0,token1,venue,pool_type,fee,stable,tick_spacing "
        "from verified_pools")
    await conn.close()
    print(f"loaded {len(rows):,} pools from verified_pools")

    pools: list[Pool] = []
    for r in rows:
        # tick_spacing is Slipstream's routing key: quoting refuses any Slipstream pool
        # without it, so dropping it here selected pools that could never be routed.
        p = Pool(r["address"], r["token0"].lower(), r["token1"].lower(),
                 r["venue"], r["pool_type"], r["fee"], r["stable"], r["tick_spacing"])
        if p.token0 in HUBS:   p.hub = p.token0
        elif p.token1 in HUBS: p.hub = p.token1
        else: continue
        pools.append(p)
    print(f"  {len(pools):,} pair a hub token ({len(rows)-len(pools):,} skipped: no hub side)")

    mc = MulticallClient(a.rpc, attempt_timeout_seconds=30.0)
    print(f"reading hub-token balances via Multicall3 (batch={a.batch}) against {a.rpc}")
    unresolved = await fetch_hub_balances(mc, pools, a.batch)
    if unresolved:
        # An unread balance is not a zero balance. Ranking them as zero drops them below
        # every floor, so a transient RPC failure would quietly remove real liquidity from
        # the selection and the run would still report success.
        print(f"\n{len(unresolved):,} of {len(pools):,} pools had no readable hub balance",
              file=sys.stderr)
        for p in unresolved[:5]:
            print(f"    {p.address}  hub={floors[p.hub][0]}", file=sys.stderr)
        if len(unresolved) > 5:
            print(f"    ... {len(unresolved)-5:,} more", file=sys.stderr)
        if not a.allow_partial_balances:
            print("refusing to curate from a partial balance read; re-run (the node may "
                  "have been busy), lower --batch, or pass --allow-partial-balances",
                  file=sys.stderr)
            return 1
        print(f"  --allow-partial-balances: excluding {len(unresolved):,} unranked pools",
              file=sys.stderr)
        pools = [p for p in pools if p.resolved]

    # Per-hub floor in native units (a WETH floor means nothing in USDC), then one
    # global sort on the USD-normalised depth so the top-N cut is economically meaningful.
    kept = [p for p in pools if p.hub_amount >= floors[p.hub][2]]
    kept.sort(key=lambda p: (-hub_usd(p), p.address))

    print(f"\n{len(kept):,} pools clear the liquidity floor:")
    by_hub: dict[str, int] = {}
    for p in kept: by_hub[floors[p.hub][0]] = by_hub.get(floors[p.hub][0], 0)+1
    for sym, n in sorted(by_hub.items(), key=lambda x: -x[1]):
        flr, usd = next((f, u) for (s_, d, f, u) in floors.values() if s_ == sym)
        print(f"    {sym:6} {n:5,}  (floor {flr:g}, ref ${usd:,.2f})")

    selected = kept[:a.top]

    # Venue mix matters more than raw count: 2-hop cycles need the same pair on two
    # different venues, so a selection that is all one venue cannot arbitrage at all.
    by_venue: dict[str, int] = {}
    for p in selected:
        by_venue[p.venue] = by_venue.get(p.venue, 0) + 1
    print(f"\nselected {len(selected)} pools by venue:")
    for v, n in sorted(by_venue.items(), key=lambda x: -x[1]):
        print(f"    {v:14} {n:5,}")
    # A venue histogram is necessary but nowhere near sufficient: the two venues have to
    # share token pairs before a single route exists. Ask the router's own enumerator.
    print(f"\nvalidating cross-venue cycles ({a.validate_hops}-hop) over the selection...")
    cycles = cross_venue_cycles(selected, a.validate_hops)
    unarbitrageable = not cycles
    if unarbitrageable:
        print(f"    ERROR: no executable cross-venue {a.validate_hops}-hop cycle exists in "
              "this selection -- the router cannot arbitrage it", file=sys.stderr)
    else:
        print(f"    {len(cycles):,} cross-venue {a.validate_hops}-hop cycle(s); first: "
              + " -> ".join(cycles[0]))

    print(f"\ntop {len(selected)} by hub-side USD depth:")
    for p in selected[:10]:
        print(f"    {p.address}  {floors[p.hub][0]:6} {p.hub_amount:,.2f}"
              f"  (${hub_usd(p):,.0f})")
    if len(selected) > 10: print(f"    ... {len(selected)-10} more")

    if a.dry_run:
        print("\n--dry-run: nothing registered")
        return 1 if unarbitrageable and not a.allow_no_cross_venue_cycle else 0
    if unarbitrageable and not a.allow_no_cross_venue_cycle:
        print("refusing to register a selection with no executable cross-venue cycle; "
              "lower the floors, raise --top, backfill a second venue, or pass "
              "--allow-no-cross-venue-cycle", file=sys.stderr)
        return 1

    # /pools/select replaces the router's pool set and persists it, rather than adding to
    # whatever a previous run left behind. One request, so the swap is atomic.
    if not a.api_token:
        print("no operator token: set OPERATOR_API_TOKEN or pass --api-token "
              "(/pools/select rewrites what the router trades and is authenticated)",
              file=sys.stderr)
        return 1

    print(f"\nselecting {len(selected)} pools at {a.api}/pools/select")
    body = []
    for p in selected:
        entry = {"address": p.address, "token0": p.token0, "token1": p.token1,
                 "venue": p.venue, "pool_type": p.pool_type}
        if p.fee is not None: entry["fee"] = p.fee
        if p.stable is not None: entry["stable"] = p.stable
        if p.tick_spacing is not None: entry["tick_spacing"] = p.tick_spacing
        body.append(entry)
    async with httpx.AsyncClient(timeout=120) as c:
        try:
            r = await c.post(
                f"{a.api}/pools/select", json=body,
                headers={"Authorization": f"Bearer {a.api_token}"},
            )
        except Exception as e:
            print(f"  selection failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    if r.status_code in (401, 503):
        print(f"  selection refused: HTTP {r.status_code} {r.text[:400]}", file=sys.stderr)
        print("  check OPERATOR_API_TOKEN matches the value the API was started with",
              file=sys.stderr)
        return 1
    if r.status_code >= 300:
        print(f"  selection rejected: HTTP {r.status_code} {r.text[:400]}", file=sys.stderr)
        return 1
    print(f"  selected={r.json().get('selected')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
