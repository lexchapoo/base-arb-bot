#!/usr/bin/env python3
"""Build an unsigned, deterministic Base deployment transaction plan.

This script never signs or broadcasts. It requires compiled Foundry artifacts and
emits exact EIP-1559 transaction payloads for an external signer to review.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import rlp
from eth_abi import encode
from eth_utils import keccak, to_checksum_address


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "contracts" / "out"


def address(value: str, label: str) -> str:
    try:
        result = to_checksum_address(value)
    except Exception as exc:
        raise SystemExit(f"{label} must be a valid address") from exc
    if int(result, 16) == 0:
        raise SystemExit(f"{label} must not be zero")
    return result


def artifact_bytecode(source: str, contract: str) -> bytes:
    path = ARTIFACTS / source / f"{contract}.json"
    if not path.exists():
        raise SystemExit(f"missing artifact {path}; run make solidity-test first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload["bytecode"]["object"]
    if raw.startswith("0x"):
        raw = raw[2:]
    return bytes.fromhex(raw)


def init_code(source: str, contract: str, types: list[str], values: list[Any]) -> bytes:
    return artifact_bytecode(source, contract) + encode(types, values)


def create_address(sender: str, nonce: int) -> str:
    encoded = rlp.encode([bytes.fromhex(sender[2:]), nonce])
    return to_checksum_address("0x" + keccak(encoded)[12:].hex())


def call_data(signature: str, types: list[str], values: list[Any]) -> str:
    selector = keccak(text=signature)[:4]
    return "0x" + (selector + encode(types, values)).hex()


def transaction(nonce: int, purpose: str, to: str | None, data: bytes | str) -> dict[str, Any]:
    encoded = "0x" + data.hex() if isinstance(data, bytes) else data
    return {"nonce": nonce, "purpose": purpose, "to": to, "value": "0x0", "data": encoded}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployer", default=os.environ.get("EXTERNAL_SIGNER_ADDRESS", ""))
    parser.add_argument("--owner", default=os.environ.get("EXECUTOR_OWNER_ADDRESS", ""))
    parser.add_argument("--nonce", required=True, type=int, help="pending nonce of the deployment signer")
    parser.add_argument("--output", type=Path, help="write the reviewed plan to a new 0600 file")
    args = parser.parse_args()

    if os.environ.get("DRY_RUN", "true").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("DRY_RUN must remain true while preparing deployment")
    if os.environ.get("LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"}:
        raise SystemExit("LIVE_TRADING must remain false while preparing deployment")
    if args.nonce < 0:
        raise SystemExit("nonce must be non-negative")

    deployer = address(args.deployer, "deployer")
    owner = address(args.owner or deployer, "owner")
    aave_pool = address(os.environ.get("AAVE_POOL", ""), "AAVE_POOL")
    aero_router = address(os.environ.get("AERODROME_ROUTER", ""), "AERODROME_ROUTER")
    aero_factory = address(os.environ.get("AERODROME_FACTORY", ""), "AERODROME_FACTORY")
    uni_router = address(os.environ.get("UNISWAP_V3_SWAP_ROUTER_02", ""), "UNISWAP_V3_SWAP_ROUTER_02")
    allowed_tokens = [
        address(value.strip(), "DEPLOY_ALLOWED_TOKENS")
        for value in os.environ.get("DEPLOY_ALLOWED_TOKENS", "").split(",")
        if value.strip()
    ]
    if not allowed_tokens:
        raise SystemExit("DEPLOY_ALLOWED_TOKENS must contain at least one verified token")

    executor = create_address(deployer, args.nonce)
    aero_adapter = create_address(deployer, args.nonce + 1)
    uni_adapter = create_address(deployer, args.nonce + 2)

    executor_init = init_code("BaseArbExecutor.sol", "BaseArbExecutor", ["address"], [aave_pool])
    aero_init = init_code(
        "AerodromeAdapter.sol", "AerodromeAdapter", ["address", "address", "address"],
        [executor, aero_router, aero_factory],
    )
    uni_init = init_code(
        "UniswapV3Adapter.sol", "UniswapV3Adapter", ["address", "address", "bool"],
        [executor, uni_router, True],
    )

    nonce = args.nonce
    transactions = [
        transaction(nonce, "deploy BaseArbExecutor (paused)", None, executor_init),
        transaction(nonce + 1, "deploy AerodromeAdapter", None, aero_init),
        transaction(nonce + 2, "deploy UniswapV3Adapter", None, uni_init),
        transaction(nonce + 3, "allow AerodromeAdapter", executor, call_data("setAdapter(address,bool)", ["address", "bool"], [aero_adapter, True])),
        transaction(nonce + 4, "allow UniswapV3Adapter", executor, call_data("setAdapter(address,bool)", ["address", "bool"], [uni_adapter, True])),
    ]
    nonce += 5
    for token in allowed_tokens:
        transactions.append(
            transaction(nonce, f"allow token {token}", executor, call_data("setToken(address,bool)", ["address", "bool"], [token, True]))
        )
        nonce += 1
    if owner != deployer:
        transactions.append(
            transaction(nonce, "start two-step ownership transfer", executor, call_data("transferOwnership(address)", ["address"], [owner]))
        )

    plan = {
        "chain_id": 8453,
        "broadcast": False,
        "activation_state": "paused",
        "deployer": deployer,
        "intended_owner": owner,
        "starting_nonce": args.nonce,
        "predicted_addresses": {
            "executor": executor,
            "aerodrome_adapter": aero_adapter,
            "uniswap_v3_adapter": uni_adapter,
        },
        "init_code_hashes": {
            "executor": "0x" + keccak(executor_init).hex(),
            "aerodrome_adapter": "0x" + keccak(aero_init).hex(),
            "uniswap_v3_adapter": "0x" + keccak(uni_init).hex(),
        },
        "transactions": transactions,
        "postconditions": [
            "verify code and constructor immutables at every predicted address",
            "verify executor.paused() is true",
            "verify adapter and token allowlists",
            "if owner differs from deployer, intended owner must separately call acceptOwnership()",
            "do not call setPaused(false) before the shadow and canary gates pass",
        ],
    }
    serialized = json.dumps(plan, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
        args.output.chmod(0o600)
        print(json.dumps({"plan_path": str(args.output), "transactions": len(transactions)}))
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
