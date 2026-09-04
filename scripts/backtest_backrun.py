#!/usr/bin/env python3
"""Did a backrunnable cross-venue spread ever exist in the flow we recorded?

Answers that offline, from `pending_pool_events` alone, before any engine work is
committed to chasing it. The router's live measurements say what it *evaluated* was
never profitable; this asks the prior question of whether the opportunity was in the
data at all.

Method, entirely from stored Swap logs -- no historical state, so a pruned node is fine:

  * Uniswap V3 Swap carries sqrtPriceX96, which IS the post-swap mid price. Exact.
  * Aerodrome Swap carries only in/out amounts, so the implied rate is an EXECUTION
    price that already includes the pool fee. It is corrected back toward mid by the
    pool's fee, which makes it an estimate, not a measurement -- flagged in the output.

Pools are grouped by token pair; for pairs quoted on more than one venue, the spread
between the most recent price on each venue is measured at each event. An opportunity
requires the spread to exceed the round-trip cost: both pool fees plus gas.

Gas on Base is small but not free, and it is charged in ETH regardless of trade size,
so the cost in basis points depends on notional. --notional-usd sets that.

  ./scripts/backtest_backrun.py --hours 24
  ./scripts/backtest_backrun.py --hours 6 --notional-usd 5000
"""
from __future__ import annotations
import argparse, asyncio, os, sys
from collections import defaultdict
from decimal import Decimal, getcontext

import asyncpg
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

getcontext().prec = 60

V3_SWAP  = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
AERO_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
Q96 = Decimal(2) ** 96

# Aerodrome does not put its fee in the log. These are the standard Base defaults and
# are only used to back out mid from an execution price; a pool with a custom fee makes
# its own estimate slightly wrong, which is why aerodrome-only pairs are reported apart.
AERO_FEE_VOLATILE = Decimal("0.003")
AERO_FEE_STABLE   = Decimal("0.0005")


def _i256(b: bytes) -> int:
    v = int.from_bytes(b, "big")
    return v - (1 << 256) if v >= (1 << 255) else v


def decode_price(topic0: str, raw: str, dec0: int, dec1: int, stable: bool | None,
                 fee: int | None) -> Decimal | None:
    """Mid price as token1 per token0, in whole units. None if undecodable."""
    try:
        b = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
    except ValueError:
        return None
    scale = Decimal(10) ** (dec0 - dec1)

    if topic0.lower() == V3_SWAP and len(b) >= 160:
        sqrt_p = int.from_bytes(b[64:96], "big")
        if sqrt_p <= 0:
            return None
        # sqrtPriceX96 is the post-swap price; squaring it needs no fee correction.
        return (Decimal(sqrt_p) / Q96) ** 2 * scale

    if topic0.lower() == AERO_SWAP and len(b) >= 128:
        a0in, a1in = int.from_bytes(b[0:32], "big"), int.from_bytes(b[32:64], "big")
        a0out, a1out = int.from_bytes(b[64:96], "big"), int.from_bytes(b[96:128], "big")
        f = AERO_FEE_STABLE if stable else AERO_FEE_VOLATILE
        if a0in > 0 and a1out > 0:
            # sold token0: received amount is net of fee, so mid is above the rate
            return (Decimal(a1out) / Decimal(a0in)) * scale / (Decimal(1) - f)
        if a1in > 0 and a0out > 0:
            # sold token1: mid is below the observed rate
            return (Decimal(a1in) / Decimal(a0out)) * scale * (Decimal(1) - f)
    return None


def pool_fee_frac(venue: str, fee: int | None, stable: bool | None) -> Decimal:
    if venue.startswith("uniswap") or "slipstream" in venue:
        return Decimal(fee or 3000) / Decimal(1_000_000)
    return AERO_FEE_STABLE if stable else AERO_FEE_VOLATILE


async def token_decimals(w3, tokens: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tokens:
        try:
            r = await w3.eth.call({"to": AsyncWeb3.to_checksum_address(t), "data": "0x313ce567"})
            out[t] = int.from_bytes(r[-32:], "big") if r else 18
        except Exception:
            out[t] = 18
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", default=os.environ.get("CURATE_PG", "postgresql://arb:arb@127.0.0.1:5432/arb"))
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--notional-usd", type=Decimal, default=Decimal("2000"),
                    help="trade size used to express fixed gas cost in basis points")
    ap.add_argument("--gas-usd", type=Decimal, default=Decimal("0.02"),
                    help="all-in gas cost of one atomic arb tx on Base")
    ap.add_argument("--exact-only", action="store_true",
                    help="compare only pools whose price is read exactly from sqrtPriceX96 "
                         "(Uniswap V3 / Slipstream). Aerodrome logs carry no price, so its "
                         "mid is reconstructed from an execution rate with an assumed fee -- "
                         "and that reconstruction is simply wrong for stable-curve pools, "
                         "which is enough to manufacture double-digit spreads that do not exist")
    ap.add_argument("--max-block-gap", type=int, default=2,
                    help="how stale the other venue's last price may be, in blocks")
    a = ap.parse_args()

    conn = await asyncpg.connect(a.pg)
    pools = {r["address"].lower(): r for r in await conn.fetch(
        "select address,token0,token1,venue,pool_type,fee,stable from verified_pools")}
    events = await conn.fetch(f"""
        select pool_address, topic0, raw_data, block_number
        from pending_pool_events
        where raw_data is not null and raw_data <> '0x' and block_number is not null
          and created_at >= now() - interval '{a.hours} hours'
        order by block_number asc""")
    await conn.close()
    print(f"loaded {len(events):,} events over {a.hours}h across {len({e['pool_address'] for e in events}):,} pools")
    if not events:
        print("no events in window"); return 1

    tokens = set()
    for e in events:
        p = pools.get(e["pool_address"].lower())
        if p: tokens |= {p["token0"].lower(), p["token1"].lower()}
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    decs = await token_decimals(w3, tokens)
    print(f"resolved decimals for {len(decs)} tokens")

    # pair -> venue-bearing pools quoting it
    pair_pools: dict[tuple[str, str], set[str]] = defaultdict(set)
    for addr, p in pools.items():
        key = tuple(sorted((p["token0"].lower(), p["token1"].lower())))
        pair_pools[key].add(addr)

    last: dict[str, tuple[int, Decimal]] = {}          # pool -> (block, price)
    best: list[tuple[Decimal, int, str, str, Decimal]] = []
    checks = wins = 0
    spread_hist: list[Decimal] = []

    for e in events:
        addr = e["pool_address"].lower()
        p = pools.get(addr)
        if not p: continue
        t0, t1 = p["token0"].lower(), p["token1"].lower()
        if a.exact_only and e["topic0"].lower() != V3_SWAP:
            continue
        price = decode_price(e["topic0"], e["raw_data"], decs.get(t0, 18), decs.get(t1, 18),
                             p["stable"], p["fee"])
        if price is None or price <= 0: continue
        blk = int(e["block_number"])
        last[addr] = (blk, price)

        key = tuple(sorted((t0, t1)))
        for other in pair_pools[key]:
            if other == addr or other not in last: continue
            oblk, oprice = last[other]
            if blk - oblk > a.max_block_gap: continue
            op = pools[other]
            if a.exact_only and not (op["venue"].startswith("uniswap") or "slipstream" in op["venue"]):
                continue
            # both prices are token1-per-token0 for the same ordered pair
            if op["token0"].lower() != t0:
                continue
            checks += 1
            hi, lo = (price, oprice) if price > oprice else (oprice, price)
            spread = (hi - lo) / lo
            spread_hist.append(spread)
            cost = (pool_fee_frac(p["venue"], p["fee"], p["stable"])
                    + pool_fee_frac(op["venue"], op["fee"], op["stable"])
                    + (a.gas_usd / a.notional_usd))
            if spread > cost:
                wins += 1
                best.append((spread - cost, blk, addr, other, spread))

    print(f"\ncross-venue comparisons: {checks:,}")
    if not checks:
        print("no pair was quoted on two venues within the block gap -- nothing to backrun")
        return 0
    spread_hist.sort()
    def pct(q): return spread_hist[min(len(spread_hist)-1, int(len(spread_hist)*q))]
    print(f"  spread bps: p50={pct(0.5)*10000:.2f}  p90={pct(0.9)*10000:.2f}  "
          f"p99={pct(0.99)*10000:.2f}  max={spread_hist[-1]*10000:.2f}")
    print(f"  net-positive after fees+gas: {wins:,} ({100*wins/checks:.3f}%)")

    if best:
        best.sort(reverse=True)
        print(f"\n  top {min(10,len(best))} by net edge:")
        for net, blk, a1, a2, sp in best[:10]:
            print(f"    block {blk}  net={net*10000:8.2f} bps  gross={sp*10000:8.2f} bps  "
                  f"{a1[:10]}… vs {a2[:10]}…")
    else:
        print("\n  no observation cleared fees + gas")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
