#!/usr/bin/env python3
"""Would the Morpho liquidation bonus survive selling the collateral?

The realised-revenue scan values a liquidation at oracle prices: seized collateral minus
debt repaid, $65,230 across 111h. That is the number the protocol pays. It is NOT the
number a liquidator keeps, because the collateral has to be sold, and the largest bonuses
sit on long-tail collateral -- exactly the assets whose exit slippage can hand the whole
bonus back.

This replays each liquidation at its real size through the router's own quote adapters:

    seized collateral --(DEX quote)--> loan token   vs   debt repaid

Routing is direct when a pool for the pair exists, otherwise via WETH, using the pools
already in `verified_pools`.

IMPORTANT -- this is not a historical replay. Node state is pruned beyond a few thousand
blocks, so quotes are taken against CURRENT liquidity at the historical trade sizes. It
answers "could a position this size be exited today", which is the structural question,
rather than "what did this specific liquidator net at the time".

  ./scripts/backtest_liquidation_exit.py --blocks 200000
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from decimal import Decimal

import asyncpg
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
AAVE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
WETH = "0x4200000000000000000000000000000000000006"
LIQ_SIG = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"

ID_TO_MARKET_PARAMS = "0x2c3c9157"
ORACLE_PRICE = "0xa035b1fe"
ADDRESSES_PROVIDER = "0x0542975c"
GET_PRICE_ORACLE = "0xfca513a8"
GET_ASSET_PRICE = "0xb3596f07"
DECIMALS = "0x313ce567"
SYMBOL = "0x95d89b41"


async def rpc(w3, m, p):
    return await w3.provider.make_request(m, p)


async def scan(w3, lo, hi, chunk):
    out, b = [], lo
    while b <= hi:
        end = min(b + chunk - 1, hi)
        r = await rpc(w3, "eth_getLogs", [{"topics": [LIQ_SIG], "fromBlock": hex(b), "toBlock": hex(end)}])
        if "result" in r:
            out.extend(r["result"])
        b = end + 1
        print(f"  scanned {end}/{hi} ({len(out)})", end="\r", flush=True)
    print(" " * 50, end="\r")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--pg", default=os.environ.get("CURATE_PG", "postgresql://arb:arb@127.0.0.1:5432/arb"))
    ap.add_argument("--blocks", type=int, default=200_000)
    ap.add_argument("--chunk", type=int, default=10_000)
    ap.add_argument("--gas-usd", type=Decimal, default=Decimal("0.05"),
                    help="gas for a flash-loan liquidation + swap on Base")
    ap.add_argument("--out", default="reports/liquidations")
    a = ap.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python", "src"))
    from arb_bot.route_engine import AdapterRegistry
    from arb_bot.event_graph import canonical_venue

    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    hi = int((await rpc(w3, "eth_blockNumber", []))["result"], 16)
    lo = max(0, hi - a.blocks)
    hours = a.blocks * 2 / 3600
    print(f"scanning {lo:,} -> {hi:,} (~{hours:.1f}h)")
    logs = await scan(w3, lo, hi, a.chunk)
    print(f"  {len(logs)} Morpho liquidations")
    if not logs:
        return 0

    # market id -> (loan, collateral, oracle)
    params = {}
    for mid in {l["topics"][1] for l in logs if len(l.get("topics", [])) >= 2}:
        r = await rpc(w3, "eth_call", [{"to": MORPHO, "data": ID_TO_MARKET_PARAMS + mid[2:]}, "latest"])
        if "result" not in r or len(r["result"]) < 200:
            continue
        b = bytes.fromhex(r["result"][2:])
        params[mid] = ("0x" + b[12:32].hex(), "0x" + b[44:64].hex(), "0x" + b[76:96].hex())

    pr = await rpc(w3, "eth_call", [{"to": AAVE_POOL, "data": ADDRESSES_PROVIDER}, "latest"])
    prov = "0x" + pr["result"][-40:]
    orc = await rpc(w3, "eth_call", [{"to": prov, "data": GET_PRICE_ORACLE}, "latest"])
    aave_oracle = "0x" + orc["result"][-40:]

    async def meta(tok):
        """(decimals, symbol, usd_price|None). Symbol and decimals come from the token
        itself, so a token Aave does not price is still identifiable in the report --
        which matters because the unroutable collateral is the interesting part."""
        try:
            d = int((await rpc(w3, "eth_call", [{"to": tok, "data": DECIMALS}, "latest"]))["result"], 16)
        except Exception:
            return None
        sym = "?"
        try:
            sy = (await rpc(w3, "eth_call", [{"to": tok, "data": SYMBOL}, "latest"]))["result"]
            raw = bytes.fromhex(sy[2:])
            if len(raw) >= 64:
                sym = raw[64:64 + int.from_bytes(raw[32:64], "big")].decode("utf8", "ignore") or "?"
        except Exception:
            pass
        px = None
        try:
            r = (await rpc(w3, "eth_call", [{"to": aave_oracle, "data": GET_ASSET_PRICE + "0" * 24 + tok[2:]}, "latest"]))["result"]
            v = int(r, 16)
            if v > 0:
                px = Decimal(v) / Decimal(10) ** 8
        except Exception:
            pass
        return d, sym, px

    toks = {t for p in params.values() for t in p[:2]}
    tmeta = {}
    for t in toks:
        m = await meta(t)
        if m:
            tmeta[t] = m

    mkt_px = {}
    for mid, (_, _, oaddr) in params.items():
        if int(oaddr, 16) == 0:
            continue
        r = await rpc(w3, "eth_call", [{"to": oaddr, "data": ORACLE_PRICE}, "latest"])
        if "result" in r and r["result"] not in ("", "0x"):
            try:
                mkt_px[mid] = int(r["result"], 16)
            except ValueError:
                pass

    # pools available to exit through
    conn = await asyncpg.connect(a.pg)
    rows = await conn.fetch("select address,token0,token1,venue,pool_type,fee,stable,tick_spacing from verified_pools")
    await conn.close()
    by_pair = {}
    for r in rows:
        k = tuple(sorted((r["token0"].lower(), r["token1"].lower())))
        by_pair.setdefault(k, []).append(r)
    print(f"  {len(rows):,} pools available for exit routing")

    registry = AdapterRegistry()

    def kw(p):
        v = canonical_venue(p["venue"])
        if v == "uniswap-v3":   return {"fee": p["fee"], "block_identifier": "latest"}
        if v == "aerodrome-slipstream": return {"tick_spacing": p["tick_spacing"], "block_identifier": "latest"}
        if v == "aerodrome":    return {"stable": p["stable"], "block_identifier": "latest"}
        return {"block_identifier": "latest"}

    async def best_quote(tin, tout, amt):
        """Best amount_out across every pool quoting the pair. None if unroutable."""
        best = None
        for p in by_pair.get(tuple(sorted((tin, tout))), []):
            ad = registry.get(p["venue"])
            if ad is None:
                continue
            try:
                q = await ad.quote_exact_input(tin, tout, amt, **kw(p))
                if q.amount_out and (best is None or q.amount_out > best):
                    best = q.amount_out
            except Exception:
                continue
        return best

    async def exit_to_loan(coll, loan, amt):
        """Direct if possible, else hop through WETH."""
        d = await best_quote(coll, loan, amt)
        if d:
            return d, "direct"
        if coll != WETH and loan != WETH:
            mid = await best_quote(coll, WETH, amt)
            if mid:
                out = await best_quote(WETH, loan, mid)
                if out:
                    return out, "via WETH"
        return None, "unroutable"

    results, gross_t, net_t = [], Decimal(0), Decimal(0)
    unroutable = 0
    print("\n  replaying exits at historical size against current liquidity")
    for i, l in enumerate(logs):
        mid = l["topics"][1] if len(l.get("topics", [])) >= 2 else None
        p = params.get(mid)
        d = bytes.fromhex(l["data"][2:]) if l.get("data") else b""
        if not p or len(d) < 96 or p[0] not in tmeta or tmeta[p[0]][2] is None:
            continue
        loan, coll, _ = p
        ldec, lsym, lusd = tmeta[loan]
        repaid_raw = int.from_bytes(d[0:32], "big")
        seized_raw = int.from_bytes(d[64:96], "big")
        if seized_raw <= 0:
            continue
        repaid_usd = Decimal(repaid_raw) / Decimal(10) ** ldec * lusd

        csym = tmeta[coll][1] if coll in tmeta else "?"
        if coll in tmeta and tmeta[coll][2] is not None:
            cdec, csym, cusd = tmeta[coll]
            seized_usd = Decimal(seized_raw) / Decimal(10) ** cdec * cusd
        elif mid in mkt_px:
            seized_usd = Decimal(seized_raw) * Decimal(mkt_px[mid]) / Decimal(10) ** 36 / Decimal(10) ** ldec * lusd
        else:
            continue
        gross = seized_usd - repaid_usd

        out_raw, route = await exit_to_loan(coll, loan, seized_raw)
        if out_raw is None:
            unroutable += 1
            results.append({"block": int(l["blockNumber"], 16), "pair": f"{lsym}<-{csym}",
                            "gross_usd": float(gross), "net_usd": None, "route": route})
            gross_t += gross
            continue
        proceeds = Decimal(out_raw) / Decimal(10) ** ldec * lusd
        net = proceeds - repaid_usd - a.gas_usd
        gross_t += gross
        net_t += net
        results.append({"block": int(l["blockNumber"], 16), "pair": f"{lsym}<-{csym}",
                        "gross_usd": float(gross), "net_usd": float(net),
                        "proceeds_usd": float(proceeds), "repaid_usd": float(repaid_usd),
                        "route": route})
        print(f"    {i+1}/{len(logs)}", end="\r", flush=True)
    print(" " * 40, end="\r")

    priced = [r for r in results if r["net_usd"] is not None]
    winners = [r for r in priced if r["net_usd"] > 0]
    priced.sort(key=lambda r: -(r["net_usd"] or 0))
    print(f"\n  replayed {len(priced)} of {len(results)} ({unroutable} unroutable)")
    print(f"  gross bonus at oracle prices : ${float(gross_t):>12,.2f}")
    print(f"  net after exit slippage+gas  : ${float(net_t):>12,.2f}")
    if gross_t:
        print(f"  slippage retained            : {float(net_t/gross_t)*100:>11.1f}%")
    print(f"  profitable after exit        : {len(winners)}/{len(priced)}")
    unr = [r for r in results if r["net_usd"] is None]
    if unr:
        from collections import Counter
        c = Counter(r["pair"] for r in unr)
        lost = sum(r["gross_usd"] for r in unr)
        print(f"\n  UNROUTABLE collateral -- ${lost:,.2f} of oracle bonus with no DEX exit:")
        for pair, n in c.most_common(8):
            amt = sum(r["gross_usd"] for r in unr if r["pair"] == pair)
            print(f"    {pair:>18}  x{n:<4} ${amt:>12,.2f}")

    print("\n  top by net:")
    for r in priced[:10]:
        print(f"    block {r['block']}  {r['pair']:>14}  gross=${r['gross_usd']:>10,.2f}  "
              f"net=${r['net_usd']:>10,.2f}  ({r['route']})")

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"exit-backtest-{hi}.json")
    with open(path, "w") as fh:
        json.dump({"from_block": lo, "to_block": hi, "hours": hours,
                   "gross_usd": float(gross_t), "net_usd": float(net_t),
                   "results": results}, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
