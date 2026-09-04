#!/usr/bin/env python3
"""Is there a liquidation business on Base Aave V3, and how big is it?

Answers that from chain history before any bot is built -- the same evidence-first step
that saved a month on the arbitrage thesis, where 30,132 live evaluations and a 24h
offline backtest agreed there was nothing to capture.

Two independent measurements:

  1. REALISED: every LiquidationCall over a window, with the collateral seized and the
     debt covered. This is revenue that actually existed, already net of whatever
     competition took it. If this is empty, nothing else matters.

  2. PIPELINE: current health factors for borrowers discovered from Borrow events.
     Aave liquidates below HF 1.0, so the distribution just above 1.0 is the queue of
     positions that a price move would push over.

Both read only the local node. Nothing is signed and nothing is sent.

  ./scripts/collect_liquidations.py --blocks 200000
  ./scripts/collect_liquidations.py --blocks 50000 --no-pipeline
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from collections import Counter
from decimal import Decimal

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

# Aave V3 Pool on Base. Same address the router already has verified in .env.
POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

BORROW_SIG = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"
LIQUIDATION_SIG = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

USER_ACCOUNT_DATA = "0xbf92857c"   # getUserAccountData(address)
AGGREGATE3 = "0x82ad56cb"
ADDRESSES_PROVIDER = "0x0542975c"
GET_PRICE_ORACLE = "0xfca513a8"
GET_ASSET_PRICE = "0xb3596f07"   # returns price in base currency (8dp USD)
DECIMALS = "0x313ce567"
SYMBOL = "0x95d89b41"

# Aave reports account values in "base currency" units, 8 decimals of USD.
BASE_DECIMALS = Decimal(10) ** 8
WAD = Decimal(10) ** 18


def _addr_from_topic(t: str) -> str:
    return "0x" + t[-40:]


async def rpc(w3, method: str, params: list):
    return await w3.provider.make_request(method, params)


async def scan_logs(w3, topic0: str, from_block: int, to_block: int, chunk: int) -> list[dict]:
    """eth_getLogs in chunks; a full-range request is refused by most nodes."""
    out: list[dict] = []
    b = from_block
    while b <= to_block:
        end = min(b + chunk - 1, to_block)
        r = await rpc(w3, "eth_getLogs", [{
            "address": POOL, "topics": [topic0],
            "fromBlock": hex(b), "toBlock": hex(end),
        }])
        if "result" in r:
            out.extend(r["result"])
        else:
            print(f"  getLogs {b}-{end} failed: {str(r.get('error'))[:120]}", file=sys.stderr)
        b = end + 1
        print(f"  scanned to {end}/{to_block} ({len(out):,} logs)", end="\r", flush=True)
    print()
    return out


def encode_multicall(calls: list[tuple[str, bytes]]) -> str:
    """aggregate3((address,bool,bytes)[]) with allowFailure=true on every call."""
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("CURATE_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--blocks", type=int, default=200_000, help="how far back to scan (~2s/block)")
    ap.add_argument("--chunk", type=int, default=10_000)
    ap.add_argument("--batch", type=int, default=200, help="borrowers per multicall")
    ap.add_argument("--no-pipeline", action="store_true", help="skip the health-factor pass")
    ap.add_argument("--state", default="reports/liquidations/borrowers.json",
                    help="accumulated borrower set + scan cursor, so the pipeline is not "
                         "re-discovered from scratch on every run")
    ap.add_argument("--seed-blocks", type=int, default=500_000,
                    help="depth of the initial borrower scan when no state file exists")
    ap.add_argument("--out", default="reports/liquidations")
    a = ap.parse_args()

    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    latest = int((await rpc(w3, "eth_blockNumber", []))["result"], 16)
    start = max(0, latest - a.blocks)
    hours = a.blocks * 2 / 3600
    print(f"scanning blocks {start:,} -> {latest:,}  (~{hours:.1f}h of history)")

    # ---- 1. realised liquidations -------------------------------------------------
    print("\n[1] LiquidationCall events")
    liqs = await scan_logs(w3, LIQUIDATION_SIG, start, latest, a.chunk)
    print(f"  {len(liqs):,} liquidations in window  "
          f"({len(liqs)/max(hours,1e-9):.2f}/hour)")

    # LiquidationCall(collateralAsset indexed, debtAsset indexed, user indexed,
    #                 debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken)
    # Non-indexed words: 0=debtToCover 1=liquidatedCollateralAmount 2=liquidator.
    liquidators: Counter = Counter()
    events = []
    for l in liqs:
        d = bytes.fromhex(l["data"][2:])
        t = l.get("topics", [])
        if len(d) < 96 or len(t) < 4:
            continue
        ev = {
            "block": int(l["blockNumber"], 16),
            "tx": l["transactionHash"],
            "collateral_asset": _addr_from_topic(t[1]),
            "debt_asset": _addr_from_topic(t[2]),
            "user": _addr_from_topic(t[3]),
            "debt_to_cover": int.from_bytes(d[0:32], "big"),
            "collateral_seized": int.from_bytes(d[32:64], "big"),
            "liquidator": "0x" + d[64:96].hex()[-40:],
        }
        liquidators[ev["liquidator"]] += 1
        events.append(ev)

    # Value both legs with Aave's own oracle -- the same prices the protocol liquidates
    # against, so the bonus we compute is the one the protocol actually paid.
    meta: dict[str, tuple[int, str, Decimal]] = {}
    assets = {e["collateral_asset"] for e in events} | {e["debt_asset"] for e in events}
    if assets:
        pr = await rpc(w3, "eth_call", [{"to": POOL, "data": ADDRESSES_PROVIDER}, "latest"])
        provider = "0x" + pr["result"][-40:]
        orc = await rpc(w3, "eth_call", [{"to": provider, "data": GET_PRICE_ORACLE}, "latest"])
        oracle = "0x" + orc["result"][-40:]
        print(f"  oracle: {oracle}")
        for asset in assets:
            try:
                dc = await rpc(w3, "eth_call", [{"to": asset, "data": DECIMALS}, "latest"])
                sy = await rpc(w3, "eth_call", [{"to": asset, "data": SYMBOL}, "latest"])
                px = await rpc(w3, "eth_call", [{"to": oracle,
                     "data": GET_ASSET_PRICE + "0" * 24 + asset[2:]}, "latest"])
                dec = int(dc["result"], 16)
                raw = bytes.fromhex(sy["result"][2:])
                sym = (raw[64:64 + int.from_bytes(raw[32:64], "big")].decode("utf8", "ignore")
                       if len(raw) >= 64 else "?")
                meta[asset] = (dec, sym or "?", Decimal(int(px["result"], 16)) / BASE_DECIMALS)
            except Exception:
                continue

    total_bonus = Decimal(0)
    for e in events:
        cm, dm = meta.get(e["collateral_asset"]), meta.get(e["debt_asset"])
        if not cm or not dm:
            continue
        e["collateral_usd"] = float(Decimal(e["collateral_seized"]) / (Decimal(10) ** cm[0]) * cm[2])
        e["debt_usd"] = float(Decimal(e["debt_to_cover"]) / (Decimal(10) ** dm[0]) * dm[2])
        e["bonus_usd"] = e["collateral_usd"] - e["debt_usd"]
        e["pair"] = f"{dm[1]}->{cm[1]}"
        total_bonus += Decimal(str(e["bonus_usd"]))

    if events:
        print("\n  per-liquidation economics (collateral seized minus debt repaid):")
        for e in sorted(events, key=lambda x: -x.get("bonus_usd", 0)):
            if "bonus_usd" not in e:
                continue
            print(f"    block {e['block']}  {e.get('pair','?'):>16}  "
                  f"debt=${e['debt_usd']:>10,.2f}  seized=${e['collateral_usd']:>10,.2f}  "
                  f"gross=${e['bonus_usd']:>9,.2f}")
        print(f"  total gross bonus in window: ${float(total_bonus):,.2f} "
              f"(${float(total_bonus)/max(hours,1e-9):,.2f}/hour)")
    if liquidators:
        print("  liquidators:")
        for who, n in liquidators.most_common(5):
            print(f"    {who}  x{n}")

    # ---- 2. borrower pipeline -----------------------------------------------------
    rows = []
    if not a.no_pipeline:
        print("\n[2] borrower pipeline")
        # Borrowers accumulate across runs. Discovering them from Borrow events inside the
        # scan window only finds addresses that happened to transact in that window, which
        # made the population -- and therefore the debt totals -- a function of --blocks
        # rather than of the market: a 1.1h window reported 26 borrowers holding $1.2M and
        # a 6h window 140 holding $46.2M, an artefact that made runs incomparable.
        # Seed once, then advance a cursor and only scan what is new.
        known: set[str] = set()
        cursor = None
        try:
            with open(a.state) as fh:
                st = json.load(fh)
            known = {u.lower() for u in st.get("borrowers", [])}
            cursor = st.get("last_scanned_block")
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

        if cursor is None:
            scan_from = max(0, latest - a.seed_blocks)
            print(f"  no state at {a.state}: seeding from {scan_from:,} "
                  f"(~{a.seed_blocks*2/86400:.1f} days)")
        else:
            # Re-scan a small overlap so a reorg near the cursor cannot drop a borrower.
            scan_from = max(0, cursor - 1_000)
            print(f"  resuming from {scan_from:,} ({len(known):,} borrowers already known)")

        borrows = await scan_logs(w3, BORROW_SIG, scan_from, latest, a.chunk)
        found = {_addr_from_topic(l["topics"][2]).lower() for l in borrows
                 if len(l.get("topics", [])) >= 3}
        new_users = found - known
        known |= found
        users = sorted(known)
        print(f"  {len(borrows):,} borrow events in range -> {len(new_users):,} new, "
              f"{len(users):,} tracked in total")

        os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
        with open(a.state, "w") as fh:
            json.dump({"last_scanned_block": latest, "borrowers": users}, fh)

        print("  reading health factors")
        for s in range(0, len(users), a.batch):
            chunk = users[s:s + a.batch]
            calls = [(POOL, bytes.fromhex(USER_ACCOUNT_DATA[2:]) + int(u, 16).to_bytes(32, "big"))
                     for u in chunk]
            r = await rpc(w3, "eth_call",
                          [{"to": MULTICALL3, "data": encode_multicall(calls)}, "latest"])
            if "result" not in r:
                print(f"  batch {s} failed: {str(r.get('error'))[:120]}", file=sys.stderr)
                continue
            for u, (ok, data) in zip(chunk, decode_multicall(r["result"])):
                if not ok or len(data) < 192:
                    continue
                coll = int.from_bytes(data[0:32], "big")
                debt = int.from_bytes(data[32:64], "big")
                hf = int.from_bytes(data[160:192], "big")
                if debt == 0:
                    continue          # no debt means nothing to liquidate
                rows.append({"user": u,
                             "collateral_usd": float(Decimal(coll) / BASE_DECIMALS),
                             "debt_usd": float(Decimal(debt) / BASE_DECIMALS),
                             "hf": float(Decimal(hf) / WAD) if hf < (1 << 255) else None})
            print(f"  health factors {min(s+a.batch,len(users))}/{len(users)}", end="\r", flush=True)
        print()

        live = [r for r in rows if r["hf"] is not None]
        live.sort(key=lambda r: r["hf"])
        total_debt = sum(r["debt_usd"] for r in live)
        print(f"  {len(live):,} borrowers with open debt, ${total_debt:,.0f} total")
        for lo, hi in [(0, 1.0), (1.0, 1.02), (1.02, 1.05), (1.05, 1.10), (1.10, 1.25)]:
            band = [r for r in live if lo <= r["hf"] < hi]
            if band:
                print(f"    HF {lo:.2f}-{hi:.2f}: {len(band):5,} positions, "
                      f"${sum(r['debt_usd'] for r in band):>12,.0f} debt")
        under = [r for r in live if r["hf"] < 1.0]
        print(f"  liquidatable right now: {len(under)}")
        for r in under[:10]:
            print(f"    {r['user']}  HF={r['hf']:.4f}  debt=${r['debt_usd']:,.0f}  "
                  f"collateral=${r['collateral_usd']:,.0f}")

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"aave-{latest}.json")
    with open(path, "w") as fh:
        json.dump({"latest_block": latest, "from_block": start, "window_hours": hours,
                   "liquidations": len(liqs), "events": events,
                   "liquidators": dict(liquidators.most_common(20)),
                   "borrowers": rows}, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
