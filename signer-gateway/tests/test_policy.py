import json

import pytest
import rlp

from signer_gateway.policy import (
    PolicyConfig,
    PolicyError,
    ReviewedDeploymentPlan,
    START_PACKED_SELECTOR,
    deployment_method_signature,
    read_secret_file,
    validate_deployment,
    validate_execution,
    verify_signed_transaction,
)


SIGNER = "0x0000000000000000000000000000000000000001"
EXECUTOR = "0x0000000000000000000000000000000000000002"
ASSET = "0x0000000000000000000000000000000000000003"


def policy(**overrides):
    values = {
        "chain_id": 8453,
        "signer_address": SIGNER,
        "executor_address": EXECUTOR,
        "max_gas": 3_000_000,
        "max_deployment_gas": 6_000_000,
        "max_fee_per_gas_wei": 5_000_000_000,
        "allow_execution_signing": True,
        "allow_deployment_signing": False,
    }
    values.update(overrides)
    return PolicyConfig(**values)


def execution_request():
    return {
        "chain_id": 8453,
        "from": SIGNER,
        "to": EXECUTOR,
        "nonce": "0x1",
        "gas": "0x493e0",
        "max_fee_per_gas": "0x3b9aca00",
        "max_priority_fee_per_gas": "0x5f5e100",
        "value": "0x0",
        "data": START_PACKED_SELECTOR + "00" * 32,
        "route_id": "0x" + "11" * 32,
        "asset": ASSET,
    }


def test_execution_policy_allows_only_executor_entrypoints():
    normalized = validate_execution(execution_request(), policy())
    assert normalized["to"] == EXECUTOR

    bad = execution_request()
    bad["data"] = "0xdeadbeef"
    with pytest.raises(PolicyError, match="selector"):
        validate_execution(bad, policy())

    with pytest.raises(PolicyError, match="disabled"):
        validate_execution(execution_request(), policy(allow_execution_signing=False))

    too_much_gas = execution_request()
    too_much_gas["gas"] = hex(4_600_000)
    with pytest.raises(PolicyError, match="gas exceeds"):
        validate_execution(too_much_gas, policy())


def test_deployment_policy_requires_exact_reviewed_plan(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "chain_id": 8453,
        "deployer": SIGNER,
        "transactions": [{
            "nonce": 7,
            "purpose": "deploy BaseArbExecutor (paused)",
            "to": None,
            "value": "0x0",
            "data": "0x60006000",
        }],
    }))
    plan_path.chmod(0o600)
    plan = ReviewedDeploymentPlan.load(plan_path)
    request = {
        "plan_hash": plan.sha256,
        "purpose": "deploy BaseArbExecutor (paused)",
        "chain_id": 8453,
        "from": SIGNER,
        "to": None,
        "nonce": "0x7",
        "gas": hex(4_600_000),
        "max_fee_per_gas": "0x3b9aca00",
        "max_priority_fee_per_gas": "0x5f5e100",
        "value": "0x0",
        "data": "0x60006000",
    }
    normalized = validate_deployment(
        request,
        policy(allow_execution_signing=False, allow_deployment_signing=True),
        plan,
    )
    assert normalized["to"] is None

    request["data"] = "0x60016000"
    with pytest.raises(PolicyError, match="calldata differs"):
        validate_deployment(
            request,
            policy(allow_execution_signing=False, allow_deployment_signing=True),
            plan,
        )


def test_deployment_method_signature_is_exactly_allowlisted():
    assert deployment_method_signature({"to": None, "data": "0x6000"}) is None
    assert deployment_method_signature({
        "to": EXECUTOR,
        "data": "0x332f6465" + "00" * 64,
    }) == "setAdapter(address,bool)"
    assert deployment_method_signature({
        "to": EXECUTOR,
        "data": "0x3816a292" + "00" * 64,
    }) == "setToken(address,bool)"
    # The proxy upgrade. Pinned by selector literal rather than recomputed from the signature,
    # so a typo in the signature string cannot silently agree with itself here and admit a
    # different method than the one reviewed.
    assert deployment_method_signature({
        "to": EXECUTOR,
        "data": "0x4f1ef286" + "00" * 128,
    }) == "upgradeToAndCall(address,bytes)"
    with pytest.raises(PolicyError, match="no reviewed ABI signature"):
        deployment_method_signature({"to": EXECUTOR, "data": "0xdeadbeef"})


def test_signed_transaction_is_compared_field_for_field(monkeypatch):
    expected = validate_execution(execution_request(), policy())
    fields = [
        expected["chain_id"],
        expected["nonce"],
        expected["max_priority_fee_per_gas"],
        expected["max_fee_per_gas"],
        expected["gas"],
        bytes.fromhex(expected["to"][2:]),
        expected["value"],
        bytes.fromhex(expected["data"][2:]),
        [],
        0,
        1,
        1,
    ]
    raw = "0x02" + rlp.encode(fields).hex()
    monkeypatch.setattr("signer_gateway.policy.Account.recover_transaction", lambda _: SIGNER)
    verify_signed_transaction(raw, expected)

    changed = dict(expected)
    changed["gas"] += 1
    with pytest.raises(PolicyError, match="gas"):
        verify_signed_transaction(raw, changed)


def test_bearer_token_file_must_be_private(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 32)
    token.chmod(0o600)
    assert read_secret_file(token) == "a" * 32
    token.chmod(0o644)
    with pytest.raises(PolicyError, match="group/world"):
        read_secret_file(token)
