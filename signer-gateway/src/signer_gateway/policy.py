from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from eth_account import Account
from eth_utils import keccak, to_checksum_address
import rlp


START_SELECTOR = "0x" + keccak(text="start((bytes32,address,uint256,uint256,uint64,uint64,(address,address,address,uint256,bytes)[]))")[:4].hex()
START_PACKED_SELECTOR = "0x" + keccak(text="startPacked(bytes)")[:4].hex()
SET_ADAPTER_SIGNATURE = "setAdapter(address,bool)"
SET_TOKEN_SIGNATURE = "setToken(address,bool)"
DEPLOYMENT_METHOD_SIGNATURES = {
    "0x" + keccak(text=SET_ADAPTER_SIGNATURE)[:4].hex(): SET_ADAPTER_SIGNATURE,
    "0x" + keccak(text=SET_TOKEN_SIGNATURE)[:4].hex(): SET_TOKEN_SIGNATURE,
}


class PolicyError(ValueError):
    pass


def normalize_address(value: str, label: str) -> str:
    try:
        result = to_checksum_address(value)
    except Exception as exc:
        raise PolicyError(f"{label} must be a valid address") from exc
    if int(result, 16) == 0:
        raise PolicyError(f"{label} must not be zero")
    return result.lower()


def parse_hex_quantity(value: str, label: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 3:
        raise PolicyError(f"{label} must be a non-empty hex quantity")
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise PolicyError(f"{label} must be a hex quantity") from exc
    if parsed < 0:
        raise PolicyError(f"{label} must not be negative")
    return parsed


def validate_hex_data(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2 != 0:
        raise PolicyError(f"{label} must be even-length 0x-prefixed hex")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PolicyError(f"{label} must be hex") from exc
    return value.lower()


def read_secret_file(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PolicyError(f"secret file {path} must not be group/world accessible")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise PolicyError("gateway bearer token must contain at least 32 characters")
    return value


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    chain_id: int
    signer_address: str
    executor_address: str
    max_gas: int
    max_deployment_gas: int
    max_fee_per_gas_wei: int
    allow_execution_signing: bool
    allow_deployment_signing: bool


@dataclass(frozen=True, slots=True)
class ReviewedDeploymentPlan:
    sha256: str
    deployer: str
    chain_id: int
    transactions: dict[int, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> "ReviewedDeploymentPlan":
        metadata = path.stat()
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PolicyError("deployment plan must not be group/world accessible")
        if metadata.st_uid != os.geteuid():
            raise PolicyError("deployment plan must be owned by the gateway user")
        raw = path.read_bytes()
        payload = json.loads(raw)
        transactions: dict[int, dict[str, Any]] = {}
        for transaction in payload.get("transactions", []):
            nonce = int(transaction["nonce"])
            if nonce in transactions:
                raise PolicyError(f"deployment plan contains duplicate nonce {nonce}")
            transactions[nonce] = transaction
        if not transactions:
            raise PolicyError("deployment plan has no transactions")
        return cls(
            sha256="0x" + hashlib.sha256(raw).hexdigest(),
            deployer=normalize_address(payload["deployer"], "deployment plan deployer"),
            chain_id=int(payload["chain_id"]),
            transactions=transactions,
        )


def validate_common(
    tx: dict[str, Any],
    policy: PolicyConfig,
    *,
    gas_cap: int | None = None,
) -> dict[str, Any]:
    if int(tx.get("chain_id", -1)) != policy.chain_id:
        raise PolicyError("wrong chain_id")
    sender = normalize_address(str(tx.get("from", "")), "from")
    if sender != policy.signer_address:
        raise PolicyError("from does not match configured signer")
    nonce = parse_hex_quantity(tx.get("nonce", ""), "nonce")
    gas = parse_hex_quantity(tx.get("gas", ""), "gas")
    priority = parse_hex_quantity(tx.get("max_priority_fee_per_gas", ""), "max_priority_fee_per_gas")
    max_fee = parse_hex_quantity(tx.get("max_fee_per_gas", ""), "max_fee_per_gas")
    value = parse_hex_quantity(tx.get("value", ""), "value")
    data = validate_hex_data(tx.get("data", ""), "data")
    effective_gas_cap = policy.max_gas if gas_cap is None else gas_cap
    if gas == 0 or gas > effective_gas_cap:
        raise PolicyError("gas exceeds signer policy")
    if max_fee == 0 or max_fee > policy.max_fee_per_gas_wei:
        raise PolicyError("max_fee_per_gas exceeds signer policy")
    if priority > max_fee:
        raise PolicyError("priority fee exceeds max fee")
    if value != 0:
        raise PolicyError("non-zero transaction value is forbidden")
    return {
        "chain_id": policy.chain_id,
        "from": sender,
        "nonce": nonce,
        "gas": gas,
        "max_priority_fee_per_gas": priority,
        "max_fee_per_gas": max_fee,
        "value": value,
        "data": data,
    }


def validate_execution(tx: dict[str, Any], policy: PolicyConfig) -> dict[str, Any]:
    if not policy.allow_execution_signing:
        raise PolicyError("execution signing is disabled")
    normalized = validate_common(tx, policy)
    destination = normalize_address(str(tx.get("to", "")), "to")
    if destination != policy.executor_address:
        raise PolicyError("execution destination is not the configured executor")
    selector = normalized["data"][:10]
    if selector not in {START_SELECTOR, START_PACKED_SELECTOR}:
        raise PolicyError("execution calldata selector is not allowed")
    route_id = validate_hex_data(tx.get("route_id", ""), "route_id")
    if len(route_id) != 66:
        raise PolicyError("route_id must be 32 bytes")
    normalize_address(str(tx.get("asset", "")), "asset")
    normalized["to"] = destination
    return normalized


def validate_deployment(
    tx: dict[str, Any],
    policy: PolicyConfig,
    plan: ReviewedDeploymentPlan | None,
) -> dict[str, Any]:
    if not policy.allow_deployment_signing:
        raise PolicyError("deployment signing is disabled")
    if plan is None:
        raise PolicyError("no reviewed deployment plan is configured")
    normalized = validate_common(tx, policy, gas_cap=policy.max_deployment_gas)
    if plan.chain_id != policy.chain_id or plan.deployer != policy.signer_address:
        raise PolicyError("deployment plan identity does not match signer policy")
    if tx.get("plan_hash", "").lower() != plan.sha256:
        raise PolicyError("deployment plan hash mismatch")
    reviewed = plan.transactions.get(normalized["nonce"])
    if reviewed is None:
        raise PolicyError("nonce is not present in reviewed deployment plan")
    requested_to = tx.get("to")
    normalized_to = None if requested_to in (None, "", "0x") else normalize_address(str(requested_to), "to")
    reviewed_to = reviewed.get("to")
    reviewed_to = None if reviewed_to in (None, "", "0x") else normalize_address(str(reviewed_to), "reviewed to")
    if normalized_to != reviewed_to:
        raise PolicyError("deployment destination differs from reviewed plan")
    if normalized["data"] != str(reviewed["data"]).lower():
        raise PolicyError("deployment calldata differs from reviewed plan")
    if str(tx.get("purpose", "")) != str(reviewed["purpose"]):
        raise PolicyError("deployment purpose differs from reviewed plan")
    normalized["to"] = normalized_to
    return normalized


def deployment_method_signature(tx: dict[str, Any]) -> str | None:
    """Return Clef's ABI hint for an exact-plan post-deployment call."""
    if tx.get("to") is None:
        return None
    signature = DEPLOYMENT_METHOD_SIGNATURES.get(tx["data"][:10])
    if signature is None:
        raise PolicyError("deployment calldata selector has no reviewed ABI signature")
    return signature


def clef_transaction(tx: dict[str, Any]) -> dict[str, str]:
    result = {
        "from": to_checksum_address(tx["from"]),
        "gas": hex(tx["gas"]),
        "maxPriorityFeePerGas": hex(tx["max_priority_fee_per_gas"]),
        "maxFeePerGas": hex(tx["max_fee_per_gas"]),
        "value": hex(tx["value"]),
        "data": tx["data"],
        "nonce": hex(tx["nonce"]),
    }
    if tx["to"] is not None:
        result["to"] = to_checksum_address(tx["to"])
    return result


def _rlp_int(value: bytes) -> int:
    return int.from_bytes(value, "big") if value else 0


def verify_signed_transaction(raw_transaction: str, expected: dict[str, Any]) -> None:
    raw = bytes.fromhex(validate_hex_data(raw_transaction, "raw_transaction")[2:])
    if not raw or raw[0] != 2:
        raise PolicyError("Clef must return an EIP-1559 type-2 transaction")
    fields = rlp.decode(raw[1:])
    if len(fields) != 12:
        raise PolicyError("invalid EIP-1559 signed transaction field count")
    if fields[8] != []:
        raise PolicyError("Clef introduced an unexpected access list")
    destination = None if fields[5] == b"" else "0x" + bytes(fields[5]).hex()
    actual = {
        "chain_id": _rlp_int(fields[0]),
        "nonce": _rlp_int(fields[1]),
        "max_priority_fee_per_gas": _rlp_int(fields[2]),
        "max_fee_per_gas": _rlp_int(fields[3]),
        "gas": _rlp_int(fields[4]),
        "to": destination,
        "value": _rlp_int(fields[6]),
        "data": "0x" + bytes(fields[7]).hex(),
    }
    for key, expected_value in expected.items():
        if key in actual and actual[key] != expected_value:
            raise PolicyError(f"Clef changed signed transaction field: {key}")
    recovered = Account.recover_transaction(raw_transaction).lower()
    if recovered != expected["from"]:
        raise PolicyError("signed transaction recovered to the wrong sender")
