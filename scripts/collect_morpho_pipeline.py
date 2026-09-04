#!/usr/bin/env python3
"""How much Morpho Blue debt is close to liquidation, market by market?

Morpho has no global health factor. Each market is an isolated (loanToken,
collateralToken, oracle, irm, lltv) tuple, so a position's health is computed against
that market's own oracle and its own LLTV:

    borrowed        = borrowShares * totalBorrowAssets / totalBorrowShares
    collateralValue = collateral * oracle.price() / 1e36        (in loan-token terms)
    maxBorrow       = collateralValue * lltv / 1e18
    health          = maxBorrow / borrowed                      (liquidatable below 1)

This matters because Morpho carries the liquidation volume on Base -- 173 events and
$65,230 of gross bonus over 111h, against Aave's 12 events and $336 -- so an Aave-only
pipeline watches the wrong book.

Borrowers accumulate across runs behind a scan cursor, same as the Aave collector: a
window-scoped population makes debt totals a function of --blocks rather than of the
market, and the runs incomparable.

Read-only. Nothing is signed and nothing is sent.

  ./scripts/collect_morpho_pipeline.py --seed-blocks 500000
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from collections import defaultdict
from decimal import Decimal

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
AAVE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

BORROW_SIG = "0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43"
POSITION = "0x93c52062"          # position(bytes32,address) -> supplyShares, borrowShares, collateral
MARKET = "0x5c60e39a"            # market(bytes32) -> totalSupplyAssets, totalSupplyShares,
                                 #                   totalBorrowAssets, totalBorrowShares, ...
ID_TO_MARKET_PARAMS = "0x2c3c9157"
ORACLE_PRICE = "0xa035b1fe"
AGGREGATE3 = "0x82ad56cb"
ADDRESSES_PROVIDER = "0x0542975c"
GET_PRICE_ORACLE = "0xfca513a8"
GET_ASSET_PRICE = "0xb3596f07"
DECIMALS = "0x313ce567"
SYMBOL = "0x95d89b41"

WAD = Decimal(10) ** 18


async def rpc(w3, m, p):
    return await w3.provider.make_request(m, p)


def encode_multicall(calls: list[tuple[str, bytes]]) -> str:
    n = len(calls)
    head = bytes.fromhex(AGGREGATE3[2:]) + (32).to_bytes(32, "big") + n.to_bytes(32, "big")
    offsets, bodies, pos = b"", b"", n * 32
    for target, data in calls:
        offsets += pos.to_bytes(32, "big")
        pad = (-len(data)) % 32
        bodies += (int(target, 16).to_bytes(32, "big") + (1).to_bytes(32, "big")
                   + (96).to_bytes(32, "big") + len(data).to_bytes(32, "big")
                   + data + b"\x00" * pad)
        pos += 128 + len(data) + pad
    return "0x" + (head + offsets + bodies).hex()


def decode_multicall(ret: str) -> list[tuple[bool, bytes]]:
    b = bytes.fromhex(ret[2:])
    n = int.from_bytes(b[32:64], "big")
    out = []
    for i in range(n):
        off = 64 + int.from_bytes(b[64 + i * 32:96 + i * 32], "big")
        ok = int.from_bytes(b[off:off + 32], "big") == 1
        dlen = int.from_bytes(b[off + 64:off + 96], "big")
        out.append((ok, b[off + 96:off + 96 + dlen]))
    return out


async def scan_logs(w3, topic0, lo, hi, chunk):
    out, b = [], lo
    while b <= hi:
        end = min(b + chunk - 1, hi)
        r = await rpc(w3, "eth_getLogs",
                      [{"address": MORPHO, "topics": [topic0],
                        "fromBlock": hex(b), "toBlock": hex(end)}])
        if "result" in r:
            out.extend(r["result"])
        else:
            print(f"  getLogs {b}-{end}: {str(r.get('error'))[:100]}", file=sys.stderr)
        b = end + 1
        print(f"  scanned {end}/{hi} ({len(out):,})", end="\r", flush=True)
    print(" " * 60, end="\r")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--chunk", type=int, default=10_000)
    ap.add_argument("--batch", type=int, default=150)
    ap.add_argument("--seed-blocks", type=int, default=500_000)
    ap.add_argument("--state", default="reports/liquidations/morpho-borrowers.json")
    ap.add_argument("--out", default="reports/liquidations")
    a = ap.parse_args()

    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    latest = int((await rpc(w3, "eth_blockNumber", []))["result"], 16)

    # ---- accumulate (market, borrower) pairs -------------------------------------
    known: set[tuple[str, str]] = set()
    cursor = None
    try:
        with open(a.state) as fh:
            st = json.load(fh)
        known = {(p[0].lower(), p[1].lower()) for p in st.get("positions", [])}
        cursor = st.get("last_scanned_block")
    except (FileNotFoundError, json.JSONDecodeError, KeyError, IndexError):
        pass

    if cursor is None:
        lo = max(0, latest - a.seed_blocks)
        print(f"no state at {a.state}: seeding from {lo:,} (~{a.seed_blocks*2/86400:.1f} days)")
    else:
        lo = max(0, cursor - 1_000)   # overlap guards against a reorg near the cursor
        print(f"resuming from {lo:,} ({len(known):,} positions already tracked)")

    logs = await scan_logs(w3, BORROW_SIG, lo, latest, a.chunk)
    # Borrow(Id indexed id, address caller, address indexed onBehalf, address indexed receiver, ...)
    found = {(l["topics"][1].lower(), "0x" + l["topics"][2][-40:].lower())
             for l in logs if len(l.get("topics", [])) >= 3}
    new = found - known
    known |= found
    print(f"  {len(logs):,} borrow events -> {len(new):,} new, {len(known):,} positions tracked")

    os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
    with open(a.state, "w") as fh:
        json.dump({"last_scanned_block": latest,
                   "positions": [list(p) for p in sorted(known)]}, fh)

    if not known:
        print("no positions to evaluate")
        return 0

    # ---- market parameters and state ---------------------------------------------
    ids = sorted({mid for mid, _ in known})
    print(f"\nreading {len(ids):,} market(s)")
    params, state, price = {}, {}, {}
    for mid in ids:
        r = await rpc(w3, "eth_call", [{"to": MORPHO, "data": ID_TO_MARKET_PARAMS + mid[2:]}, "latest"])
        if "result" not in r or len(r["result"]) < 322:
            continue
        b = bytes.fromhex(r["result"][2:])
        params[mid] = {"loan": "0x" + b[12:32].hex(), "coll": "0x" + b[44:64].hex(),
                       "oracle": "0x" + b[76:96].hex(), "lltv": int.from_bytes(b[128:160], "big")}
        m = await rpc(w3, "eth_call", [{"to": MORPHO, "data": MARKET + mid[2:]}, "latest"])
        if "result" in m and len(m["result"]) >= 130:
            mb = bytes.fromhex(m["result"][2:])
            state[mid] = {"tba": int.from_bytes(mb[64:96], "big"),
                          "tbs": int.from_bytes(mb[96:128], "big")}
        o = params[mid]["oracle"]
        if int(o, 16):
            pr = await rpc(w3, "eth_call", [{"to": o, "data": ORACLE_PRICE}, "latest"])
            if "result" in pr and pr["result"] not in ("", "0x"):
                try:
                    price[mid] = int(pr["result"], 16)
                except ValueError:
                    pass

    # loan-token USD prices, for reporting debt in dollars
    pr = await rpc(w3, "eth_call", [{"to": AAVE_POOL, "data": ADDRESSES_PROVIDER}, "latest"])
    prov = "0x" + pr["result"][-40:]
    orc = await rpc(w3, "eth_call", [{"to": prov, "data": GET_PRICE_ORACLE}, "latest"])
    aave_oracle = "0x" + orc["result"][-40:]
    tmeta = {}
    for t in {p["loan"] for p in params.values()}:
        try:
            d = int((await rpc(w3, "eth_call", [{"to": t, "data": DECIMALS}, "latest"]))["result"], 16)
            sy = (await rpc(w3, "eth_call", [{"to": t, "data": SYMBOL}, "latest"]))["result"]
            raw = bytes.fromhex(sy[2:])
            sym = raw[64:64 + int.from_bytes(raw[32:64], "big")].decode("utf8", "ignore") if len(raw) >= 64 else "?"
            px = int((await rpc(w3, "eth_call", [{"to": aave_oracle, "data": GET_ASSET_PRICE + "0" * 24 + t[2:]}, "latest"]))["result"], 16)
            tmeta[t] = (d, sym or "?", Decimal(px) / Decimal(10) ** 8 if px > 0 else None)
        except Exception:
            continue

    # ---- positions ----------------------------------------------------------------
    pos = sorted(known)
    print(f"reading {len(pos):,} position(s)")
    rows = []
    for s in range(0, len(pos), a.batch):
        chunk = pos[s:s + a.batch]
        calls = [(MORPHO, bytes.fromhex(POSITION[2:]) + bytes.fromhex(mid[2:])
                  + int(user, 16).to_bytes(32, "big")) for mid, user in chunk]
        r = await rpc(w3, "eth_call", [{"to": MULTICALL3, "data": encode_multicall(calls)}, "latest"])
        if "result" not in r:
            print(f"  batch {s} failed: {str(r.get('error'))[:110]}", file=sys.stderr)
            continue
        for (mid, user), (ok, data) in zip(chunk, decode_multicall(r["result"])):
            if not ok or len(data) < 96 or mid not in params or mid not in state:
                continue
            borrow_shares = int.from_bytes(data[32:64], "big")
            collateral = int.from_bytes(data[64:96], "big")
            if borrow_shares == 0:
                continue
            st_ = state[mid]
            if st_["tbs"] == 0:
                continue
            borrowed = Decimal(borrow_shares) * Decimal(st_["tba"]) / Decimal(st_["tbs"])
            if borrowed <= 0 or mid not in price:
                continue
            coll_value = Decimal(collateral) * Decimal(price[mid]) / (Decimal(10) ** 36)
            max_borrow = coll_value * Decimal(params[mid]["lltv"]) / WAD
            health = float(max_borrow / borrowed) if borrowed else None
            loan = params[mid]["loan"]
            usd = None
            if loan in tmeta and tmeta[loan][2] is not None:
                usd = float(borrowed / (Decimal(10) ** tmeta[loan][0]) * tmeta[loan][2])
            rows.append({"market": mid, "user": user, "health": health,
                         "debt_usd": usd, "loan": tmeta.get(loan, (0, "?", None))[1]})
        print(f"  positions {min(s+a.batch,len(pos))}/{len(pos)}", end="\r", flush=True)
    print(" " * 50, end="\r")

    live = [r for r in rows if r["health"] is not None]
    live.sort(key=lambda r: r["health"])
    priced = [r for r in live if r["debt_usd"] is not None]
    total = sum(r["debt_usd"] for r in priced)
    print(f"\n  {len(live):,} open borrows, ${total:,.0f} debt priced "
          f"({len(live)-len(priced)} in markets Aave does not price)")
    for lo_, hi_ in [(0, 1.0), (1.0, 1.02), (1.02, 1.05), (1.05, 1.10), (1.10, 1.25)]:
        band = [r for r in live if lo_ <= r["health"] < hi_]
        if band:
            d = sum(r["debt_usd"] for r in band if r["debt_usd"] is not None)
            print(f"    health {lo_:.2f}-{hi_:.2f}: {len(band):5,} positions, ${d:>14,.0f} debt")
    under = [r for r in live if r["health"] < 1.0]
    print(f"  liquidatable right now: {len(under)}")
    for r in under[:10]:
        print(f"    {r['user']}  health={r['health']:.4f}  "
              f"debt=${r['debt_usd']:,.0f}" if r["debt_usd"] is not None else
              f"    {r['user']}  health={r['health']:.4f}")

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"morpho-pipeline-{latest}.json")
    with open(path, "w") as fh:
        json.dump({"block": latest, "positions": rows}, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
