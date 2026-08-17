#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import sys
import httpx
from eth_utils import keccak

RPC=os.environ.get("BASE_HTTP_RPC","")
EXPECTED=int(os.environ.get("CHAIN_ID","8453"))
MARKET_NAMES=[
    "AAVE_POOL","AERODROME_ROUTER","AERODROME_FACTORY","UNISWAP_V3_QUOTER_V2",
    "UNISWAP_V3_SWAP_ROUTER_02",
    "BASE_WETH","GAS_PRICE_ORACLE",
]
DEPLOYMENT_NAMES=["EXECUTOR_ADDRESS","AERODROME_ADAPTER_ADDRESS","UNISWAP_V3_ADAPTER_ADDRESS"]

def rpc(method,params):
    r=httpx.post(RPC,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=10)
    r.raise_for_status(); body=r.json()
    if body.get("error"): raise RuntimeError(body["error"])
    return body["result"]

def eth_call(address, signature):
    selector="0x"+keccak(text=signature)[:4].hex()
    return rpc("eth_call",[{"to":address,"data":selector},"pending"])

def decode_address(value):
    return "0x"+value[-40:].lower()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--market-data-only",action="store_true")
    args=parser.parse_args()
    if not RPC:
        print("BASE_HTTP_RPC missing",file=sys.stderr); return 2
    chain=int(rpc("eth_chainId",[]),16)
    if chain != EXPECTED:
        print(f"wrong chain: expected {EXPECTED}, got {chain}",file=sys.stderr); return 3
    failed=False
    print(f"chain_id={chain} verified")
    names=MARKET_NAMES if args.market_data_only else MARKET_NAMES+DEPLOYMENT_NAMES
    for name in names:
        value=os.environ.get(name,"").strip()
        if not value:
            print(f"{name}=MISSING")
            failed=True
            continue
        if not value.startswith("0x") or len(value)!=42:
            print(f"{name}=INVALID_ADDRESS")
            failed=True
            continue
        code=rpc("eth_getCode",[value,"pending"])
        if name=="BASE_WETH":
            # WETH is a contract too, so it is verified like every other runtime dependency.
            pass
        ok=code not in ("0x","0x0",None)
        print(f"{name}={value} bytecode={'ok' if ok else 'missing'}")
        failed |= not ok
    for value in filter(None, (v.strip() for v in os.environ.get("DEPLOY_ALLOWED_TOKENS", "").split(","))):
        if not value.startswith("0x") or len(value) != 42:
            print("DEPLOY_ALLOWED_TOKENS=INVALID_ADDRESS")
            failed = True
            continue
        code = rpc("eth_getCode", [value, "pending"])
        ok = code not in ("0x", "0x0", None)
        print(f"DEPLOY_ALLOWED_TOKEN={value} bytecode={'ok' if ok else 'missing'}")
        failed |= not ok
    watched={value.strip().lower() for value in os.environ.get("WATCHED_POOL_ADDRESSES","").split(",") if value.strip()}
    try:
        metadata=json.loads(os.environ.get("WATCHED_POOLS_JSON","[]"))
    except json.JSONDecodeError as exc:
        print(f"WATCHED_POOLS_JSON=INVALID_JSON:{exc}")
        metadata=[]; failed=True
    metadata_addresses={str(pool.get("address","")).lower() for pool in metadata if isinstance(pool,dict)}
    if watched != metadata_addresses:
        print("WATCHED_POOLS_JSON=WATCHLIST_MISMATCH")
        failed=True
    for pool in metadata:
        address=str(pool.get("address","")).lower()
        token0=str(pool.get("token0","")).lower()
        token1=str(pool.get("token1","")).lower()
        if not address.startswith("0x") or len(address)!=42:
            print("WATCHED_POOL=INVALID_ADDRESS"); failed=True; continue
        code=rpc("eth_getCode",[address,"pending"])
        chain_token0=decode_address(eth_call(address,"token0()"))
        chain_token1=decode_address(eth_call(address,"token1()"))
        ok=code not in ("0x","0x0",None) and chain_token0==token0 and chain_token1==token1
        if pool.get("venue")=="uniswap-v3":
            chain_fee=int(eth_call(address,"fee()"),16)
            ok &= chain_fee==pool.get("fee")
        elif pool.get("venue")=="aerodrome":
            chain_stable=bool(int(eth_call(address,"stable()"),16))
            ok &= chain_stable is pool.get("stable")
        else:
            ok=False
        print(f"WATCHED_POOL={address} metadata={'ok' if ok else 'mismatch'}")
        failed |= not ok
    for topic in filter(None,(value.strip() for value in os.environ.get("WATCHED_TOPIC0S","").split(","))):
        ok=topic.startswith("0x") and len(topic)==66
        print(f"WATCHED_TOPIC0={topic} format={'ok' if ok else 'invalid'}")
        failed |= not ok
    return 1 if failed else 0

if __name__=="__main__": raise SystemExit(main())
