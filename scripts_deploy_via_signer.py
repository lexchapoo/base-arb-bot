#!/usr/bin/env python3
"""Preview or execute a reviewed Base deployment through the external signer gateway.

This script never handles a private key. Without --broadcast it performs validation
and RPC estimation only; it does not request signatures or submit transactions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any

from eth_utils import keccak, to_checksum_address
import httpx


def rpc(url: str, method: str, params: list[Any]) -> Any:
    response = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def hex_int(value: str) -> int:
    return int(value, 16)


def read_token(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError("signer bearer token file must have 0600 permissions")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("signer bearer token must contain at least 32 characters")
    return token


def signer_deployment_url() -> str:
    explicit = os.environ.get("SIGNER_DEPLOYMENT_URL", "").strip()
    if explicit:
        return explicit
    execution_url = os.environ.get("EXTERNAL_SIGNER_URL", "").strip()
    if execution_url.endswith("/sign"):
        return execution_url[:-5] + "/sign-deployment"
    raise RuntimeError("SIGNER_DEPLOYMENT_URL is required")


def function_selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def address_word(address: str) -> str:
    return "0" * 24 + address.lower().removeprefix("0x")


def call_word(rpc_url: str, contract: str, data: str) -> str:
    result = rpc(rpc_url, "eth_call", [{"to": contract, "data": data}, "latest"])
    if not isinstance(result, str) or len(result) != 66:
        raise RuntimeError("contract getter returned an invalid ABI word")
    return result.lower()


def call_address(rpc_url: str, contract: str, signature: str) -> str:
    word = call_word(rpc_url, contract, function_selector(signature))
    return to_checksum_address("0x" + word[-40:])


def call_bool(rpc_url: str, contract: str, signature: str, address: str | None = None) -> bool:
    data = function_selector(signature)
    if address is not None:
        data += address_word(address)
    value = hex_int(call_word(rpc_url, contract, data))
    if value not in (0, 1):
        raise RuntimeError(f"{signature} returned a non-boolean ABI value")
    return value == 1


def constructor_words(transaction: dict[str, Any], count: int) -> list[str]:
    data = str(transaction["data"]).removeprefix("0x")
    suffix_length = count * 64
    if len(data) < suffix_length:
        raise RuntimeError("deployment init code is too short for constructor arguments")
    suffix = data[-suffix_length:]
    return [suffix[index:index + 64] for index in range(0, suffix_length, 64)]


def word_address(word: str) -> str:
    if len(word) != 64 or int(word[:24], 16) != 0:
        raise RuntimeError("constructor address argument is not canonically encoded")
    return to_checksum_address("0x" + word[-40:])


def verify_deployed_prefix(rpc_url: str, plan: dict[str, Any], resume_nonce: int) -> None:
    starting_nonce = int(plan["starting_nonce"])
    upgradeable = bool(plan.get("upgradeable"))
    # A phase-2 plan begins at the proxy because the implementation is already on-chain, so its
    # contract prefix is the same length as the immutable plan's.
    predeployed_impl = bool(plan.get("implementation_predeployed"))
    prefix_length = 4 if (upgradeable and not predeployed_impl) else 3
    if resume_nonce != starting_nonce + prefix_length:
        raise RuntimeError(
            f"resume is permitted only after the exact {prefix_length}-contract deployment prefix"
        )
    prefix = plan["transactions"][:prefix_length]
    if [int(item["nonce"]) for item in prefix] != list(range(starting_nonce, resume_nonce)):
        raise RuntimeError("deployment plan does not contain the expected contiguous prefix")
    expected_purposes = [
        "deploy ERC1967Proxy + initialize (paused)",
        "deploy AerodromeAdapter",
        "deploy UniswapV3Adapter",
    ] if (upgradeable and predeployed_impl) else [
        "deploy BaseArbExecutorUpgradeable implementation",
        "deploy ERC1967Proxy + initialize (paused)",
        "deploy AerodromeAdapter",
        "deploy UniswapV3Adapter",
    ] if upgradeable else [
        "deploy BaseArbExecutor (paused)",
        "deploy AerodromeAdapter",
        "deploy UniswapV3Adapter",
    ]
    if [item["purpose"] for item in prefix] != expected_purposes:
        raise RuntimeError("deployment plan prefix purposes are not recognized")

    addresses = plan["predicted_addresses"]
    executor = to_checksum_address(addresses["executor"])
    aerodrome = to_checksum_address(addresses["aerodrome_adapter"])
    uniswap = to_checksum_address(addresses["uniswap_v3_adapter"])
    code_checks = [("executor", executor), ("aerodrome_adapter", aerodrome), ("uniswap_v3_adapter", uniswap)]
    if upgradeable:
        code_checks.append(("implementation", to_checksum_address(addresses["implementation"])))
    for label, address in code_checks:
        if rpc(rpc_url, "eth_getCode", [address, "latest"]) in ("0x", "0x0"):
            raise RuntimeError(f"resume verification found no bytecode for {label}")

    if upgradeable:
        # `executor` is the proxy. Pin the slot it delegates to, or a resume could continue
        # against a proxy that is already pointing somewhere else entirely.
        actual_impl = call_address(rpc_url, executor, "implementation()")
        if actual_impl.lower() != addresses["implementation"].lower():
            raise RuntimeError(
                f"resume verification: proxy delegates to {actual_impl}, expected {addresses['implementation']}"
            )
        # POOL is set by initialize(), not a constructor, so a zero here means the proxy was
        # deployed but never initialized -- it would be claimable by whoever calls initialize.
        pool = call_address(rpc_url, executor, "POOL()")
        if int(pool, 16) == 0:
            raise RuntimeError("resume verification: proxy POOL() is zero -- initialize did not run")
        adapter_prefix = prefix[1:3] if predeployed_impl else prefix[2:4]
    else:
        pool = word_address(constructor_words(prefix[0], 1)[0])
        adapter_prefix = prefix[1:3]

    aero_executor, aero_router, aero_factory = map(word_address, constructor_words(adapter_prefix[0], 3))
    uni_words = constructor_words(adapter_prefix[1], 3)
    uni_executor, uni_router = map(word_address, uni_words[:2])
    uni_router02 = int(uni_words[2], 16)
    if uni_router02 not in (0, 1):
        raise RuntimeError("Uniswap router02 constructor argument is not boolean")

    deployer = to_checksum_address(plan["deployer"])
    expected_addresses = {
        "executor owner": (call_address(rpc_url, executor, "owner()"), deployer),
        **({} if upgradeable else {"executor pool": (call_address(rpc_url, executor, "POOL()"), pool)}),
        "Aerodrome executor": (call_address(rpc_url, aerodrome, "executor()"), aero_executor),
        "Aerodrome router": (call_address(rpc_url, aerodrome, "router()"), aero_router),
        "Aerodrome factory": (call_address(rpc_url, aerodrome, "factory()"), aero_factory),
        "Uniswap executor": (call_address(rpc_url, uniswap, "executor()"), uni_executor),
        "Uniswap router": (call_address(rpc_url, uniswap, "router()"), uni_router),
    }
    for label, (actual, expected) in expected_addresses.items():
        if actual.lower() != expected.lower():
            raise RuntimeError(f"resume verification mismatch for {label}")
    if aero_executor.lower() != executor.lower() or uni_executor.lower() != executor.lower():
        raise RuntimeError("adapter constructor does not bind to the predicted executor")
    if not call_bool(rpc_url, executor, "paused()"):
        raise RuntimeError("resume verification requires the executor to remain paused")
    if call_bool(rpc_url, uniswap, "router02()") != bool(uni_router02):
        raise RuntimeError("resume verification mismatch for Uniswap router02")
    for adapter in (aerodrome, uniswap):
        if call_bool(rpc_url, executor, "adapterAllowed(address)", adapter):
            raise RuntimeError("adapter allowlist changed before the requested resume nonce")


def wait_receipt(rpc_url: str, tx_hash: str, timeout_seconds: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        receipt = rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            if hex_int(receipt.get("status", "0x0")) != 1:
                raise RuntimeError(f"deployment transaction reverted: {tx_hash}")
            return receipt
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for receipt: {tx_hash}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--resume-from-nonce", type=int)
    args = parser.parse_args()

    if os.environ.get("DRY_RUN", "true").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("DRY_RUN must remain true during contract deployment")
    if os.environ.get("LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"}:
        raise SystemExit("LIVE_TRADING must remain false during contract deployment")
    if args.broadcast and os.environ.get("DEPLOYMENT_BROADCAST_ACK") != "I_UNDERSTAND_BASE_DEPLOYMENT":
        raise SystemExit("set DEPLOYMENT_BROADCAST_ACK=I_UNDERSTAND_BASE_DEPLOYMENT for --broadcast")

    raw_plan = args.plan.read_bytes()
    plan = json.loads(raw_plan)
    plan_hash = "0x" + hashlib.sha256(raw_plan).hexdigest()
    rpc_url = os.environ.get("BASE_HTTP_RPC", "").strip()
    if not rpc_url:
        raise SystemExit("BASE_HTTP_RPC is required")
    if hex_int(rpc(rpc_url, "eth_chainId", [])) != int(plan["chain_id"]):
        raise SystemExit("RPC chain does not match deployment plan")
    if int(plan["chain_id"]) != 8453:
        raise SystemExit("only Base Mainnet chain ID 8453 is allowed")

    deployer = to_checksum_address(plan["deployer"])
    pending_nonce = hex_int(rpc(rpc_url, "eth_getTransactionCount", [deployer, "pending"]))
    starting_nonce = int(plan["starting_nonce"])
    if args.resume_from_nonce is None and pending_nonce != starting_nonce:
        raise SystemExit(
            f"pending nonce changed: plan={plan['starting_nonce']} chain={pending_nonce}; regenerate plan"
        )
    if args.resume_from_nonce is not None:
        if pending_nonce != args.resume_from_nonce:
            raise SystemExit(
                f"resume nonce mismatch: requested={args.resume_from_nonce} chain={pending_nonce}"
            )
        verify_deployed_prefix(rpc_url, plan, args.resume_from_nonce)

    priority = hex_int(rpc(rpc_url, "eth_maxPriorityFeePerGas", []))
    pending_block = rpc(rpc_url, "eth_getBlockByNumber", ["pending", False])
    base_fee = hex_int(pending_block["baseFeePerGas"])
    max_fee = base_fee * 2 + priority
    prepared: list[dict[str, Any]] = []
    partial_after: int | None = None
    for transaction in plan["transactions"]:
        if int(transaction["nonce"]) < pending_nonce:
            continue
        tx = {
            "from": deployer,
            "value": transaction["value"],
            "data": transaction["data"],
        }
        if transaction.get("to"):
            tx["to"] = transaction["to"]
        # A call to an address with no code does not revert -- it succeeds for ~22k gas and
        # returns a confident, meaningless estimate. That is how a plan whose admin calls target
        # a proxy created earlier in the same plan gets gas limits less than half what the real
        # calls need, failing on-chain only after the money is spent. Estimation cannot detect
        # this, so check for code explicitly.
        if transaction.get("to"):
            if rpc(rpc_url, "eth_getCode", [transaction["to"], "pending"]) in ("0x", "0x0"):
                if int(transaction["nonce"]) == pending_nonce:
                    raise SystemExit(
                        f"transaction {transaction['nonce']} ({transaction['purpose']}) targets "
                        f"{transaction['to']}, which has no code. Any estimate for it would be "
                        f"meaningless. Deploy that contract first, then generate a plan for these "
                        f"calls against the deployed address."
                    )
                partial_after = int(transaction["nonce"])
                break
        try:
            estimated = hex_int(rpc(rpc_url, "eth_estimateGas", [tx, "pending"]))
        except RuntimeError as exc:
            # A proxy deployment cannot be estimated before its implementation exists: the
            # ERC1967 constructor reverts with ERC1967InvalidImplementation. The same is true of
            # any later call whose target is created earlier in this same plan. Refusing to guess
            # a gas limit, we prepare only the prefix that current chain state can actually
            # estimate and require a follow-up --resume-from-nonce run once it has landed.
            if int(transaction["nonce"]) == pending_nonce:
                raise SystemExit(
                    f"cannot estimate the next transaction (nonce {pending_nonce}, "
                    f"{transaction['purpose']}): {exc}"
                ) from exc
            partial_after = int(transaction["nonce"])
            break
        gas = (estimated * 120 + 99) // 100
        prepared.append({
            "plan_hash": plan_hash,
            "purpose": transaction["purpose"],
            "chain_id": 8453,
            "from": deployer,
            "to": transaction.get("to"),
            "nonce": hex(int(transaction["nonce"])),
            "gas": hex(gas),
            "max_fee_per_gas": hex(max_fee),
            "max_priority_fee_per_gas": hex(priority),
            "value": transaction["value"],
            "data": transaction["data"],
        })

    if not args.broadcast:
        print(json.dumps({
            "broadcast": False,
            "plan_hash": plan_hash,
            "deployer": deployer,
            "resume_from_nonce": args.resume_from_nonce,
            "skipped_transactions": pending_nonce - starting_nonce,
            "transactions": [
                {"nonce": item["nonce"], "purpose": item["purpose"], "to": item["to"], "gas": item["gas"]}
                for item in prepared
            ],
        }, indent=2))
        return 0

    token_path_raw = os.environ.get("EXTERNAL_SIGNER_BEARER_TOKEN_FILE", "").strip()
    if not token_path_raw:
        raise SystemExit("EXTERNAL_SIGNER_BEARER_TOKEN_FILE is required")
    token_path = Path(token_path_raw)
    token = read_token(token_path)
    signer_url = signer_deployment_url()
    tx_hashes: list[str] = []
    for item in prepared:
        current_nonce = hex_int(rpc(rpc_url, "eth_getTransactionCount", [deployer, "pending"]))
        if current_nonce != hex_int(item["nonce"]):
            raise RuntimeError("pending nonce changed during sequential deployment")
        response = httpx.post(
            signer_url,
            json=item,
            headers={"Authorization": f"Bearer {token}"},
            timeout=330,
        )
        response.raise_for_status()
        raw_transaction = response.json().get("raw_transaction", "")
        if not raw_transaction.startswith("0x"):
            raise RuntimeError("signer gateway returned invalid raw transaction")
        tx_hash = rpc(rpc_url, "eth_sendRawTransaction", [raw_transaction])
        wait_receipt(rpc_url, tx_hash)
        tx_hashes.append(tx_hash)

    for label, address in plan["predicted_addresses"].items():
        if rpc(rpc_url, "eth_getCode", [address, "latest"]) in ("0x", "0x0"):
            raise RuntimeError(f"missing deployed bytecode for {label}")
    paused_selector = "0x" + keccak(text="paused()")[:4].hex()
    paused = hex_int(rpc(rpc_url, "eth_call", [{
        "to": plan["predicted_addresses"]["executor"],
        "data": paused_selector,
    }, "latest"]))
    if paused != 1:
        raise RuntimeError("deployed executor is not paused")

    print(json.dumps({
        "broadcast": True,
        "plan_hash": plan_hash,
        "tx_hashes": tx_hashes,
        "predicted_addresses": plan["predicted_addresses"],
        "executor_paused": True,
        "resume_from_nonce": args.resume_from_nonce,
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
