"""The two-phase plan that upgrades the live UUPS proxy.

Split into phases because `upgradeToAndCall` cannot be gas-estimated while its target
implementation does not yet exist -- ERC1967 reverts with ERC1967InvalidImplementation, and
scripts_deploy_via_signer refuses to guess a limit for a transaction it cannot estimate. Each
phase is therefore estimable against real chain state before a signature is requested.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from eth_abi import decode
from eth_utils import keccak

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts_prepare_deployment.py"

PROXY = "0x23e49388ab7a758ae6d582fe6fa3e52b6c64caeb"
OWNER = "0x341938c405773fc8ae3980c07ca5a516855ed7a2"
IMPL = "0x000000000000000000000000000000000000dead"
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"


def _load():
    spec = importlib.util.spec_from_file_location("prepare_deployment", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"prepare_deployment dependencies unavailable: {exc}")
    return module


def _plan(implementation="", nonce=9):
    module = _load()
    return module.upgrade_plan(
        proxy=module.to_checksum_address(PROXY),
        deployer=module.to_checksum_address(OWNER),
        owner=module.to_checksum_address(OWNER),
        morpho=module.to_checksum_address(MORPHO),
        implementation=module.to_checksum_address(implementation) if implementation else "",
        nonce=nonce,
    )


def test_phase_one_deploys_logic_and_touches_no_proxy():
    """Nothing is pointed at the new code yet, so this phase cannot break the live contract."""
    plan = _plan()
    assert plan["upgrade_phase"] == 1
    assert len(plan["transactions"]) == 1
    tx = plan["transactions"][0]
    assert tx["to"] is None, "phase 1 must be a plain deploy, not a call"
    assert tx["value"] == "0x0"
    assert len(tx["data"]) > 2_000, "implementation init code looks implausibly small"


def test_phase_two_is_a_single_call_to_the_proxy():
    plan = _plan(implementation=IMPL)
    assert plan["upgrade_phase"] == 2
    assert plan["implementation_predeployed"] is True
    assert len(plan["transactions"]) == 1
    tx = plan["transactions"][0]
    assert tx["to"].lower() == PROXY
    assert tx["data"].startswith("0x" + keccak(text="upgradeToAndCall(address,bytes)")[:4].hex())


def test_phase_two_carries_setmorpho_as_its_payload():
    """Upgrade and configure atomically: no window where MORPHO is zero and routes revert.

    upgradeToAndCall delegatecalls the payload against the new implementation right after moving
    the pointer, and delegatecall preserves msg.sender, so setMorpho's onlyOwner sees the owner
    who sent the upgrade.
    """
    tx = _plan(implementation=IMPL)["transactions"][0]
    implementation, payload = decode(["address", "bytes"], bytes.fromhex(tx["data"][10:]))
    assert implementation.lower() == IMPL

    assert payload[:4] == keccak(text="setMorpho(address)")[:4]
    (target,) = decode(["address"], payload[4:])
    assert target.lower() == MORPHO


def test_the_payload_is_never_initialize():
    """initialize is spent on a live proxy; using it as the payload reverts the whole upgrade."""
    tx = _plan(implementation=IMPL)["transactions"][0]
    _, payload = decode(["address", "bytes"], bytes.fromhex(tx["data"][10:]))
    assert payload[:4] != keccak(text="initialize(address,address)")[:4]


def test_upgrading_a_proxy_to_itself_is_refused():
    """A proxy delegating to itself is an infinite loop, and unrecoverable."""
    module = _load()
    with pytest.raises(SystemExit):
        module.upgrade_plan(
            proxy=module.to_checksum_address(PROXY),
            deployer=module.to_checksum_address(OWNER),
            owner=module.to_checksum_address(OWNER),
            morpho=module.to_checksum_address(MORPHO),
            implementation=module.to_checksum_address(PROXY),
            nonce=9,
        )


def test_the_plan_starts_at_the_nonce_it_was_given():
    """scripts_deploy_via_signer refuses to run if the chain nonce has moved past this."""
    plan = _plan(implementation=IMPL, nonce=42)
    assert plan["starting_nonce"] == 42
    assert plan["transactions"][0]["nonce"] == 42


def test_the_plan_does_not_broadcast_or_arm_anything():
    for plan in (_plan(), _plan(implementation=IMPL)):
        assert plan["broadcast"] is False
        assert plan["activation_state"] == "unchanged", "an upgrade must not unpause the executor"
        assert plan["chain_id"] == 8453


def test_postconditions_name_the_check_that_proves_the_layout_held():
    """owner() reading back correctly is the whole safety argument; it must be written down."""
    conditions = " ".join(_plan(implementation=IMPL)["postconditions"]).lower()
    assert "owner()" in conditions
    assert "paused()" in conditions
    assert "morpho()" in conditions
    assert OWNER in conditions
