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
# Overridable so the plan can be generated from a writable artifact directory when the default
# out/ is not usable (e.g. left root-owned by a prior container/tooling run).
ARTIFACTS = Path(os.environ.get("DEPLOY_ARTIFACTS_DIR") or (ROOT / "contracts" / "out"))


ZERO_ADDRESS = "0x" + "0" * 40


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


def emit(plan: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(plan, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        # "x" mode: never silently overwrite a plan a signer may already be pinned to.
        with output.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
        output.chmod(0o600)
        print(json.dumps({"plan_path": str(output), "transactions": len(plan["transactions"])}))
    else:
        print(serialized, end="")


def transaction(nonce: int, purpose: str, to: str | None, data: bytes | str) -> dict[str, Any]:
    encoded = "0x" + data.hex() if isinstance(data, bytes) else data
    return {"nonce": nonce, "purpose": purpose, "to": to, "value": "0x0", "data": encoded}


def upgrade_plan(*, proxy: str, deployer: str, owner: str, morpho: str,
                 implementation: str, nonce: int) -> dict[str, Any]:
    """Upgrade an already-deployed UUPS proxy in place, in two independently estimable phases.

    Phase 1 (no --implementation) deploys the new logic contract and nothing else. Phase 2
    passes that address back in and emits the single upgrade call.

    Split, rather than one plan of two transactions, because `upgradeToAndCall` cannot be
    estimated while its target implementation does not yet exist: ERC1967 reverts with
    ERC1967InvalidImplementation, and a plan whose next transaction cannot be estimated is
    exactly what the signer refuses to guess a gas limit for.

    Phase 2 is a *single* transaction that both upgrades and configures. upgradeToAndCall
    delegatecalls its payload against the new implementation immediately after moving the
    pointer, and delegatecall preserves msg.sender, so setMorpho's onlyOwner sees the owner who
    sent the upgrade. That leaves no window in which the proxy is upgraded but MORPHO is still
    zero -- a window in which every Morpho route would revert.

    The payload is deliberately not `initialize`: that is spent on a live proxy and would revert,
    taking the upgrade with it.
    """
    if implementation and implementation.lower() == proxy.lower():
        raise SystemExit("--implementation must not be the proxy itself")
    if not implementation:
        return {
            "chain_id": 8453,
            "broadcast": False,
            "activation_state": "unchanged",
            "admin_only": False,
            "deployer": deployer,
            "intended_owner": owner,
            "starting_nonce": nonce,
            "upgrade_target": proxy,
            "upgrade_phase": 1,
            "predicted_addresses": {"implementation": create_address(deployer, nonce)},
            "init_code_hashes": {},
            "transactions": [
                transaction(
                    nonce, "deploy BaseArbExecutorUpgradeable implementation", None,
                    init_code("BaseArbExecutorUpgradeable.sol", "BaseArbExecutorUpgradeable", [], []),
                )
            ],
            "postconditions": [
                "this plan touches no proxy: it only puts new logic on chain, inert until pointed at",
                "verify the deployed implementation's owner() is zero -- a bare implementation "
                "must not be initializable, or whoever initializes it owns it and can upgrade it",
                "record the deployed address and re-run with --implementation <address> for phase 2",
            ],
        }

    # A zero MORPHO here is always a mistake, and a silent one. Zero is a legitimate value in
    # a fresh deployment -- it means "this deployment does not use Morpho" -- so `address()`
    # never sees it and MORPHO_ADDRESS being absent from .env costs nothing there. But this
    # plan exists only to give a live proxy Morpho support, and it spends the owner nonce on
    # `setMorpho(0)`: the upgrade lands, every Morpho route is refused, and the postcondition
    # reads "verify executor.MORPHO() is 0x0" as though that were the intent. Reopening exactly
    # the window the two-phase split was built to close, via an unset variable.
    if morpho == ZERO_ADDRESS:
        raise SystemExit(
            "phase 2 upgrades the proxy to gain Morpho support, so MORPHO_ADDRESS must be set: "
            "setMorpho(0) would burn the nonce and leave every Morpho route refused"
        )

    # Phase 2. The payload runs as the owner via delegatecall; see the docstring.
    payload = call_data("setMorpho(address)", ["address"], [morpho])
    return {
        "chain_id": 8453,
        "broadcast": False,
        "activation_state": "unchanged",
        "admin_only": True,
        "deployer": deployer,
        "intended_owner": owner,
        "starting_nonce": nonce,
        "upgrade_target": proxy,
        "upgrade_phase": 2,
        "implementation_predeployed": True,
        "predicted_addresses": {"executor": proxy, "implementation": implementation},
        "init_code_hashes": {},
        "transactions": [
            transaction(
                nonce, "upgradeToAndCall(implementation, setMorpho) on the live proxy", proxy,
                call_data(
                    "upgradeToAndCall(address,bytes)", ["address", "bytes"],
                    [implementation, bytes.fromhex(payload[2:])],
                ),
            )
        ],
        "postconditions": [
            f"verify executor.owner() is still {owner} -- that read is what proves the storage "
            "layout survived; a shifted layout returns the old pendingOwner, usually zero",
            "verify executor.POOL() is unchanged",
            "verify executor.paused() is unchanged -- a shifted layout can also silently unpause",
            f"verify executor.MORPHO() is {morpho}",
            "verify executor.implementation() is the address deployed in phase 1",
            "run scripts_verify_runtime.py with FLASH_PROVIDER=morpho; it must exit 0",
            "the signer must BE the owner: upgradeToAndCall is onlyOwner, so a mismatch shows up "
            "as a failed gas estimate rather than a wasted nonce",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployer", default=os.environ.get("EXTERNAL_SIGNER_ADDRESS", ""))
    parser.add_argument("--owner", default=os.environ.get("EXECUTOR_OWNER_ADDRESS", ""))
    parser.add_argument("--nonce", required=True, type=int, help="pending nonce of the deployment signer")
    parser.add_argument("--output", type=Path, help="write the reviewed plan to a new 0600 file")
    parser.add_argument(
        "--executor", default="",
        help="address of an ALREADY-DEPLOYED executor. Emits ONLY the adapter/token allowlist "
             "calls against it, so every transaction targets live code and estimates correctly. "
             "Requires --aerodrome-adapter and --uniswap-v3-adapter.",
    )
    parser.add_argument("--aerodrome-adapter", default="", help="deployed AerodromeAdapter address")
    parser.add_argument("--uniswap-v3-adapter", default="", help="deployed UniswapV3Adapter address")
    parser.add_argument(
        "--implementation", default="",
        help="address of an ALREADY-DEPLOYED BaseArbExecutorUpgradeable implementation. Use for "
             "the second phase of a proxy deployment: the plan then starts at the ERC1967 proxy, "
             "so every transaction in it can be estimated against real chain state. Requires "
             "--upgradeable.",
    )
    parser.add_argument(
        "--upgrade-proxy", default="",
        help="address of an ALREADY-DEPLOYED ERC1967 proxy to upgrade in place. Two phases, "
             "each independently estimable: run it alone to emit the implementation deploy, "
             "then again with --implementation <deployed> to emit the upgrade itself.",
    )
    parser.add_argument(
        "--upgradeable", action="store_true",
        help="deploy BaseArbExecutorUpgradeable behind an ERC1967 (UUPS) proxy instead of the "
             "immutable BaseArbExecutor. The owner key then also controls upgrades.",
    )
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
    # Optional second flash-loan provider. Unlike every other address here, zero is a valid
    # value: it means "this deployment does not use Morpho", and the executor then refuses
    # Morpho routes rather than falling back to Aave. address() rejects zero, so it cannot be
    # used for a knowingly-empty field.
    morpho_raw = os.environ.get("MORPHO_ADDRESS", "").strip()
    morpho = address(morpho_raw, "MORPHO_ADDRESS") if morpho_raw else ZERO_ADDRESS
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

    if args.implementation and not (args.upgradeable or args.upgrade_proxy):
        raise SystemExit("--implementation requires --upgradeable or --upgrade-proxy")

    if args.upgrade_proxy:
        if args.executor:
            raise SystemExit("--upgrade-proxy and --executor are separate operations")
        proxy = address(args.upgrade_proxy, "--upgrade-proxy")
        plan = upgrade_plan(
            proxy=proxy, deployer=deployer, owner=owner, morpho=morpho,
            implementation=address(args.implementation, "--implementation") if args.implementation else "",
            nonce=args.nonce,
        )
        emit(plan, args.output)
        return 0

    if args.executor:
        # Allowlist-only plan. The executor and both adapters already exist on-chain, so nothing
        # here depends on a contract created later in the same plan -- the failure mode where a
        # call to a not-yet-deployed address estimates at ~22k instead of its real ~55k.
        if not (args.aerodrome_adapter and args.uniswap_v3_adapter):
            raise SystemExit("--executor requires --aerodrome-adapter and --uniswap-v3-adapter")
        executor = address(args.executor, "--executor")
        aero_adapter = address(args.aerodrome_adapter, "--aerodrome-adapter")
        uni_adapter = address(args.uniswap_v3_adapter, "--uniswap-v3-adapter")
        nonce = args.nonce
        transactions = [
            transaction(nonce, "allow AerodromeAdapter", executor,
                        call_data("setAdapter(address,bool)", ["address", "bool"], [aero_adapter, True])),
            transaction(nonce + 1, "allow UniswapV3Adapter", executor,
                        call_data("setAdapter(address,bool)", ["address", "bool"], [uni_adapter, True])),
        ]
        nonce += 2
        for token in allowed_tokens:
            transactions.append(
                transaction(nonce, f"allow token {token}", executor,
                            call_data("setToken(address,bool)", ["address", "bool"], [token, True]))
            )
            nonce += 1
        plan = {
            "chain_id": 8453,
            "broadcast": False,
            "activation_state": "paused",
            "admin_only": True,
            "deployer": deployer,
            "intended_owner": owner,
            "starting_nonce": args.nonce,
            "predicted_addresses": {
                "executor": executor,
                "aerodrome_adapter": aero_adapter,
                "uniswap_v3_adapter": uni_adapter,
            },
            "init_code_hashes": {},
            "transactions": transactions,
            "postconditions": [
                "verify executor.paused() is true",
                "verify every adapter and token in this plan reads back as allowed on-chain",
                "this plan deploys nothing; it only mutates the allowlist of an existing executor",
                "do not call setPaused(false) before the shadow and canary gates pass",
            ],
        }
        emit(plan, args.output)
        return 0

    if args.upgradeable and args.implementation:
        # Phase 2: the implementation is already on-chain, so this plan begins at the proxy.
        # Nothing here depends on a contract created later in the same plan, which means every
        # transaction is estimable against real state before a single signature is requested.
        implementation = address(args.implementation, "--implementation")
        executor = create_address(deployer, args.nonce)
        aero_adapter = create_address(deployer, args.nonce + 1)
        uni_adapter = create_address(deployer, args.nonce + 2)
        implementation_init = None
        initialize_data = bytes.fromhex(
            call_data("initialize(address,address)", ["address", "address"], [aave_pool, morpho])[2:]
        )
        executor_init = init_code(
            "ERC1967Proxy.sol", "ERC1967Proxy", ["address", "bytes"],
            [implementation, initialize_data],
        )
        deploy_steps = [("deploy ERC1967Proxy + initialize (paused)", executor_init)]
    elif args.upgradeable:
        # impl -> proxy -> adapters. The proxy is the executor: every later call and the
        # EXECUTOR_ADDRESS the bot uses must target the proxy, never the implementation.
        implementation = create_address(deployer, args.nonce)
        executor = create_address(deployer, args.nonce + 1)
        aero_adapter = create_address(deployer, args.nonce + 2)
        uni_adapter = create_address(deployer, args.nonce + 3)
        implementation_init = init_code(
            "BaseArbExecutorUpgradeable.sol", "BaseArbExecutorUpgradeable", [], []
        )
        # initialize(pool) runs in the proxy's context via the ERC1967 constructor, so the proxy
        # can never be left uninitialized and claimable in a separate transaction.
        initialize_data = bytes.fromhex(
            call_data("initialize(address,address)", ["address", "address"], [aave_pool, morpho])[2:]
        )
        executor_init = init_code(
            "ERC1967Proxy.sol", "ERC1967Proxy", ["address", "bytes"],
            [implementation, initialize_data],
        )
        deploy_steps = [
            ("deploy BaseArbExecutorUpgradeable implementation", implementation_init),
            ("deploy ERC1967Proxy + initialize (paused)", executor_init),
        ]
    else:
        implementation = None
        implementation_init = None
        executor = create_address(deployer, args.nonce)
        aero_adapter = create_address(deployer, args.nonce + 1)
        uni_adapter = create_address(deployer, args.nonce + 2)
        executor_init = init_code(
            "BaseArbExecutor.sol", "BaseArbExecutor", ["address", "address"], [aave_pool, morpho]
        )
        deploy_steps = [("deploy BaseArbExecutor (paused)", executor_init)]
    aero_init = init_code(
        "AerodromeAdapter.sol", "AerodromeAdapter", ["address", "address", "address"],
        [executor, aero_router, aero_factory],
    )
    uni_init = init_code(
        "UniswapV3Adapter.sol", "UniswapV3Adapter", ["address", "address", "bool"],
        [executor, uni_router, True],
    )

    nonce = args.nonce
    transactions = []
    for purpose, init in deploy_steps:
        transactions.append(transaction(nonce, purpose, None, init))
        nonce += 1
    transactions.append(transaction(nonce, "deploy AerodromeAdapter", None, aero_init))
    transactions.append(transaction(nonce + 1, "deploy UniswapV3Adapter", None, uni_init))
    transactions.append(transaction(nonce + 2, "allow AerodromeAdapter", executor, call_data("setAdapter(address,bool)", ["address", "bool"], [aero_adapter, True])))
    transactions.append(transaction(nonce + 3, "allow UniswapV3Adapter", executor, call_data("setAdapter(address,bool)", ["address", "bool"], [uni_adapter, True])))
    nonce += 4
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
        "upgradeable": bool(args.upgradeable),
        "implementation_predeployed": bool(args.upgradeable and args.implementation),
        "predicted_addresses": {
            "executor": executor,
            **({"implementation": implementation} if implementation else {}),
            "aerodrome_adapter": aero_adapter,
            "uniswap_v3_adapter": uni_adapter,
        },
        "init_code_hashes": {
            "executor": "0x" + keccak(executor_init).hex(),
            **({"implementation": "0x" + keccak(implementation_init).hex()} if implementation_init else {}),
            "aerodrome_adapter": "0x" + keccak(aero_init).hex(),
            "uniswap_v3_adapter": "0x" + keccak(uni_init).hex(),
        },
        "transactions": transactions,
        "postconditions": [
            "verify code and constructor immutables at every predicted address",
            "verify executor.paused() is true",
            "if upgradeable: EXECUTOR_ADDRESS must be the PROXY, never the implementation",
            "if upgradeable: verify the implementation cannot be initialized (owner() == 0)",
            "if upgradeable: the owner key also controls upgradeToAndCall -- treat it as a total-loss key",
            "verify adapter and token allowlists",
            "if owner differs from deployer, intended owner must separately call acceptOwnership()",
            "do not call setPaused(false) before the shadow and canary gates pass",
        ],
    }
    emit(plan, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
