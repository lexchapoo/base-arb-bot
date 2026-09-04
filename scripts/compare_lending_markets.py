#!/usr/bin/env python3
"""Which Base lending market actually pays liquidators?

The Aave-only measurement found $336 of gross bonus over 4.6 days, 98% of it from two
events -- a verdict that only holds if Aave is where the borrowing is. Moonwell and
Morpho Blue are the other two large Base markets, so this counts realised liquidations
across all three over the same window before any build decision is made.

Filters by event topic with no address filter, so every Moonwell mToken market and every
Morpho Blue market is caught without needing to enumerate them first.

Sizing differs per protocol and is deliberately NOT normalised here:
  * Aave      -- both legs valued via the protocol's own oracle (see collect_liquidations.py)
  * Moonwell  -- repayAmount is in the mToken's underlying; seizeTokens is in mTokens
  * Morpho    -- repaidAssets/seizedAssets are in that market's loan/collateral token
Counts and participant sets are directly comparable; dollar values are only computed for
whichever market turns out to carry the activity.

  ./scripts/compare_lending_markets.py --blocks 200000
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from collections import Counter

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

PROTOCOLS = {
    "aave-v3":  "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286",
    "moonwell": "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52",
    "morpho":   "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41",
}
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
AAVE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"

ID_TO_MARKET_PARAMS = "0x2c3c9157"
ORACLE_PRICE = "0xa035b1fe"        # Morpho IOracle.price()   # Morpho: id -> (loan, collateral, oracle, irm, lltv)
ADDRESSES_PROVIDER = "0x0542975c"
GET_PRICE_ORACLE = "0xfca513a8"
GET_ASSET_PRICE = "0xb3596f07"       # Aave oracle, 8dp USD -- used as a common price source
DECIMALS = "0x313ce567"
SYMBOL = "0x95d89b41"


async def _aave_oracle(w3) -> str:
    pr = await rpc(w3, "eth_call", [{"to": AAVE_POOL, "data": ADDRESSES_PROVIDER}, "latest"])
    provider = "0x" + pr["result"][-40:]
    orc = await rpc(w3, "eth_call", [{"to": provider, "data": GET_PRICE_ORACLE}, "latest"])
    return "0x" + orc["result"][-40:]


async def _token_meta(w3, oracle: str, token: str):
    """(decimals, symbol, usd_price) or None when Aave does not price the token."""
    try:
        dc = await rpc(w3, "eth_call", [{"to": token, "data": DECIMALS}, "latest"])
        sy = await rpc(w3, "eth_call", [{"to": token, "data": SYMBOL}, "latest"])
        px = await rpc(w3, "eth_call",
                       [{"to": oracle, "data": GET_ASSET_PRICE + "0" * 24 + token[2:]}, "latest"])
        dec = int(dc["result"], 16)
        raw = bytes.fromhex(sy["result"][2:])
        sym = (raw[64:64 + int.from_bytes(raw[32:64], "big")].decode("utf8", "ignore")
               if len(raw) >= 64 else "?")
        price = int(px["result"], 16) / 1e8
        if price <= 0:
            return None
        return dec, sym or "?", price
    except Exception:
        return None


async def size_morpho(w3, logs: list[dict]) -> None:
    """Value Morpho liquidations: seized collateral minus repaid debt, in USD.

    Morpho markets are identified by an id, so the token pair has to be resolved per
    market. Prices come from Aave's oracle because it is already trusted here and covers
    the majors; a market whose tokens Aave does not price is counted and skipped rather
    than valued with a guess.
    """
    if not logs:
        return
    oracle = await _aave_oracle(w3)
    ids = {l["topics"][1] for l in logs if len(l.get("topics", [])) >= 2}
    params: dict[str, tuple] = {}
    for mid in ids:
        r = await rpc(w3, "eth_call",
                      [{"to": MORPHO, "data": ID_TO_MARKET_PARAMS + mid[2:]}, "latest"])
        if "result" not in r or len(r["result"]) < 66:
            continue
        b = bytes.fromhex(r["result"][2:])
        params[mid] = ("0x" + b[12:32].hex(), "0x" + b[44:64].hex())

    # Each Morpho market carries its own oracle, quoting collateral in LOAN-token terms
    # scaled by 1e36 * 10^(loanDec - collDec). Using it instead of Aave's price list is
    # what makes the long-tail markets valuable at all: Aave priced only 5 of 173.
    mkt_price: dict[str, int] = {}
    for mid, (loan, coll) in params.items():
        r = await rpc(w3, "eth_call",
                      [{"to": MORPHO, "data": ID_TO_MARKET_PARAMS + mid[2:]}, "latest"])
        if "result" not in r or len(r["result"]) < 200:
            continue
        oracle_addr = "0x" + bytes.fromhex(r["result"][2:])[76:96].hex()
        if int(oracle_addr, 16) == 0:
            continue
        pr = await rpc(w3, "eth_call", [{"to": oracle_addr, "data": ORACLE_PRICE}, "latest"])
        if "result" in pr and pr["result"] not in ("", "0x"):
            try:
                mkt_price[mid] = int(pr["result"], 16)
            except ValueError:
                pass

    tokens = {t for pair in params.values() for t in pair}
    meta = {}
    for t in tokens:
        m = await _token_meta(w3, oracle, t)
        if m:
            meta[t] = m

    total, priced, unpriced = 0.0, 0, 0
    rows = []
    for l in logs:
        mid = l["topics"][1] if len(l.get("topics", [])) >= 2 else None
        pair = params.get(mid)
        d = bytes.fromhex(l["data"][2:]) if l.get("data") else b""
        if not pair or len(d) < 96:
            continue
        loan, coll = pair
        if loan not in meta:
            unpriced += 1          # loan token is the USD anchor; without it, no valuation
            continue
        ldec, lsym, lusd = meta[loan]
        repaid_raw = int.from_bytes(d[0:32], "big")
        seized_raw = int.from_bytes(d[64:96], "big")
        repaid = repaid_raw / 10 ** ldec * lusd
        if coll in meta:
            cdec, csym, cusd = meta[coll]
            seized = seized_raw / 10 ** cdec * cusd
        elif mid in mkt_price:
            # seized (collateral units) -> loan units via the market oracle, then to USD
            seized = seized_raw * mkt_price[mid] / 1e36 / 10 ** ldec * lusd
            csym = "?"
        else:
            unpriced += 1
            continue
        rows.append((seized - repaid, repaid, seized, f"{lsym}->{csym}",
                     int(l["blockNumber"], 16)))
        total += seized - repaid
        priced += 1

    rows.sort(reverse=True)
    print(f"  valued {priced} of {len(logs)} ({unpriced} in markets Aave does not price)")
    print(f"  top liquidations by gross bonus:")
    for bonus, repaid, seized, pair, blk in rows[:8]:
        print(f"    block {blk}  {pair:>16}  repaid=${repaid:>11,.2f}  "
              f"seized=${seized:>11,.2f}  gross=${bonus:>10,.2f}")
    print(f"  total gross bonus (priced subset): ${total:,.2f}")


async def rpc(w3, method, params):
    return await w3.provider.make_request(method, params)


async def scan(w3, topic0: str, lo: int, hi: int, chunk: int) -> list[dict]:
    out, b = [], lo
    while b <= hi:
        end = min(b + chunk - 1, hi)
        r = await rpc(w3, "eth_getLogs",
                      [{"topics": [topic0], "fromBlock": hex(b), "toBlock": hex(end)}])
        if "result" in r:
            out.extend(r["result"])
        else:
            print(f"    {b}-{end} failed: {str(r.get('error'))[:110]}", file=sys.stderr)
        b = end + 1
        print(f"    {end}/{hi} ({len(out):,})", end="\r", flush=True)
    print(" " * 60, end="\r")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--blocks", type=int, default=200_000)
    ap.add_argument("--chunk", type=int, default=10_000)
    ap.add_argument("--out", default="reports/liquidations")
    a = ap.parse_args()

    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    hi = int((await rpc(w3, "eth_blockNumber", []))["result"], 16)
    lo = max(0, hi - a.blocks)
    hours = a.blocks * 2 / 3600
    print(f"scanning {lo:,} -> {hi:,}  (~{hours:.1f}h)\n")

    summary = {}
    for name, topic in PROTOCOLS.items():
        print(f"[{name}]")
        logs = await scan(w3, topic, lo, hi, a.chunk)

        # Attribute by emitting contract: Aave and Morpho emit from one address, Moonwell
        # from each mToken, so the market count is itself a signal of breadth.
        venues = Counter(l["address"].lower() for l in logs)
        callers: Counter = Counter()
        for l in logs:
            t = l.get("topics", [])
            d = bytes.fromhex(l["data"][2:]) if l.get("data") else b""
            if name == "aave-v3" and len(d) >= 96:
                callers["0x" + d[64:96].hex()[-40:]] += 1
            elif name == "morpho" and len(t) >= 3:
                callers["0x" + t[2][-40:]] += 1          # caller is indexed
            elif name == "moonwell" and len(d) >= 32:
                callers["0x" + d[0:32].hex()[-40:]] += 1  # liquidator is first word
        summary[name] = {"count": len(logs), "per_hour": len(logs) / max(hours, 1e-9),
                         "markets": len(venues), "liquidators": len(callers)}
        if name == "morpho":
            await size_morpho(w3, logs)
        print(f"  {len(logs):,} liquidations  ({len(logs)/max(hours,1e-9):.2f}/hour)  "
              f"across {len(venues)} emitting contract(s)  by {len(callers)} liquidator(s)")
        for who, n in callers.most_common(5):
            print(f"    {who}  x{n}")
        print()

    print("=" * 62)
    print(f"  {'protocol':12} {'liquidations':>13} {'per hour':>10} {'liquidators':>12}")
    for name, s in sorted(summary.items(), key=lambda kv: -kv[1]["count"]):
        print(f"  {name:12} {s['count']:>13,} {s['per_hour']:>10.2f} {s['liquidators']:>12,}")

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"markets-{hi}.json")
    with open(path, "w") as fh:
        json.dump({"from_block": lo, "to_block": hi, "hours": hours, "summary": summary}, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
