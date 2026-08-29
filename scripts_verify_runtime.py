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
PROVIDERS={"aave","morpho"}
# keccak256("MORPHO()")[:4] -- the executor's view of which second provider it was deployed
# with. Hardcoded rather than derived so this script keeps its httpx-only dependency.
MORPHO_SELECTOR="0x3acb5624"

def rpc(method,params):
    r=httpx.post(RPC,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=10)
    r.raise_for_status(); body=r.json()
    if body.get("error"): raise RuntimeError(body["error"])
    return body["result"]

def decode_address(word):
    """Last 20 bytes of an ABI-encoded word, lowercased for comparison."""
    if not word or word in ("0x","0x0"):
        return None
    return "0x"+word[-40:].lower()


def main():
    if not RPC:
        print("BASE_HTTP_RPC missing",file=sys.stderr); return 2
    chain=int(rpc("eth_chainId",[]),16)
    if chain != EXPECTED:
        print(f"wrong chain: expected {EXPECTED}, got {chain}",file=sys.stderr); return 3
    failed=False
    print(f"chain_id={chain} verified")

    provider=os.environ.get("FLASH_PROVIDER","aave").strip().lower() or "aave"
    if provider not in PROVIDERS:
        print(f"FLASH_PROVIDER={provider} is not one of {sorted(PROVIDERS)}",file=sys.stderr)
        return 4
    print(f"flash_provider={provider}")

    names=list(NAMES)
    morpho=os.environ.get("MORPHO_ADDRESS","").strip()
    if provider=="morpho":
        # Only required on the Morpho path. Both the executor and the finalizer already refuse
        # to borrow without it, so this turns a run that would have blocked every route into a
        # failure at preflight, where it is visible.
        names.append("MORPHO_ADDRESS")
    elif morpho:
        # Not in use, but a typo here is a trap armed for whenever the switch is flipped.
        names.append("MORPHO_ADDRESS")

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

    # The env var and the deployed executor must name the same Morpho, or the bot prices routes
    # against liquidity it will not actually borrow from -- and if the executor holds zero, every
    # Morpho route reverts with ProviderNotConfigured after passing every other check here.
    if provider=="morpho" and not failed:
        executor=os.environ.get("EXECUTOR_ADDRESS","").strip()
        try:
            onchain=decode_address(rpc("eth_call",[{"to":executor,"data":MORPHO_SELECTOR},"pending"]))
        except Exception as exc:
            print(f"EXECUTOR_MORPHO=UNREADABLE ({type(exc).__name__}) -- "
                  "executor predates the second-provider upgrade?",file=sys.stderr)
            return 5
        if onchain is None or int(onchain,16)==0:
            print("EXECUTOR_MORPHO=UNSET -- executor was deployed without a Morpho address; "
                  "every Morpho route will revert",file=sys.stderr)
            return 5
        if onchain != morpho.lower():
            print(f"EXECUTOR_MORPHO={onchain} does not match MORPHO_ADDRESS={morpho.lower()}",
                  file=sys.stderr)
            return 5
        print(f"EXECUTOR_MORPHO={onchain} matches MORPHO_ADDRESS")
    return 1 if failed else 0

if __name__=="__main__": raise SystemExit(main())
