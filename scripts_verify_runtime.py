#!/usr/bin/env python3
from __future__ import annotations
import os
import sys
import httpx

RPC=os.environ.get("BASE_HTTP_RPC","")
EXPECTED=int(os.environ.get("CHAIN_ID","8453"))
NAMES=[
    "EXECUTOR_ADDRESS","AAVE_POOL","AERODROME_ROUTER","AERODROME_FACTORY",
    "AERODROME_ADAPTER_ADDRESS","UNISWAP_V3_ADAPTER_ADDRESS","UNISWAP_V3_QUOTER_V2",
    "BASE_WETH","GAS_PRICE_ORACLE",
]

def rpc(method,params):
    r=httpx.post(RPC,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=10)
    r.raise_for_status(); body=r.json()
    if body.get("error"): raise RuntimeError(body["error"])
    return body["result"]

def main():
    if not RPC:
        print("BASE_HTTP_RPC missing",file=sys.stderr); return 2
    chain=int(rpc("eth_chainId",[]),16)
    if chain != EXPECTED:
        print(f"wrong chain: expected {EXPECTED}, got {chain}",file=sys.stderr); return 3
    failed=False
    print(f"chain_id={chain} verified")
    for name in NAMES:
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
    return 1 if failed else 0

if __name__=="__main__": raise SystemExit(main())
