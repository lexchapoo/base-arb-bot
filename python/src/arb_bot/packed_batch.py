from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

PACKED_VERSION = 1
MAX_PACKED_CANDIDATES = 8
MAX_PACKED_LEGS = 4
MAX_PACKED_LEG_DATA = 2048


def _hex_bytes(value: str, size: int, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"{label} must be 0x-prefixed hex")
    raw = bytes.fromhex(value[2:])
    if len(raw) != size:
        raise ValueError(f"{label} must be {size} bytes")
    return raw


def _u(value: int, size: int, label: str) -> bytes:
    if not isinstance(value, int) or value < 0 or value >= 1 << (size * 8):
        raise ValueError(f"{label} does not fit uint{size * 8}")
    return value.to_bytes(size, "big")


@dataclass(frozen=True, slots=True)
class PackedLeg:
    adapter: str
    token_in: str
    token_out: str
    min_out: int
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class PackedCandidate:
    candidate_id: str
    min_profit: int
    legs: tuple[PackedLeg, ...]


@dataclass(frozen=True, slots=True)
class PackedBatch:
    asset: str
    amount: int
    target_block: int
    deadline: int
    candidates: tuple[PackedCandidate, ...]

    @property
    def batch_hash(self) -> str:
        from web3 import Web3
        value = Web3.keccak(encode_packed_batch(self)).hex()
        return value if value.startswith("0x") else "0x" + value


def candidate_id(route_id: str) -> str:
    """Stable 32-byte candidate commitment from an existing route id/string."""
    if route_id.startswith("0x") and len(route_id) == 66:
        _hex_bytes(route_id, 32, "route_id")
        return route_id.lower()
    return "0x" + hashlib.sha256(route_id.encode()).hexdigest()


def encode_packed_batch(batch: PackedBatch) -> bytes:
    candidates = tuple(batch.candidates)
    if not 1 <= len(candidates) <= MAX_PACKED_CANDIDATES:
        raise ValueError("candidate count out of bounds")

    out = bytearray()
    out += _u(PACKED_VERSION, 1, "version")
    out += _u(len(candidates), 1, "candidate_count")
    out += _hex_bytes(batch.asset, 20, "asset")
    out += _u(batch.amount, 32, "amount")
    out += _u(batch.target_block, 8, "target_block")
    out += _u(batch.deadline, 8, "deadline")

    for candidate in candidates:
        if not 2 <= len(candidate.legs) <= MAX_PACKED_LEGS:
            raise ValueError("leg count out of bounds")
        out += _hex_bytes(candidate.candidate_id, 32, "candidate_id")
        out += _u(candidate.min_profit, 32, "min_profit")
        out += _u(len(candidate.legs), 1, "leg_count")
        for leg in candidate.legs:
            if leg.token_in.lower() == leg.token_out.lower():
                raise ValueError("token_in and token_out must differ")
            if len(leg.data) > MAX_PACKED_LEG_DATA:
                raise ValueError("leg data too large")
            out += _hex_bytes(leg.adapter, 20, "adapter")
            out += _hex_bytes(leg.token_in, 20, "token_in")
            out += _hex_bytes(leg.token_out, 20, "token_out")
            out += _u(leg.min_out, 32, "min_out")
            out += _u(len(leg.data), 2, "data_len")
            out += leg.data
    return bytes(out)


def _take(raw: bytes, offset: int, size: int) -> tuple[bytes, int]:
    end = offset + size
    if end > len(raw):
        raise ValueError("truncated packed batch")
    return raw[offset:end], end


def decode_packed_batch(raw: bytes) -> PackedBatch:
    offset = 0
    b, offset = _take(raw, offset, 1)
    if b[0] != PACKED_VERSION:
        raise ValueError("unsupported packed version")
    b, offset = _take(raw, offset, 1)
    count = b[0]
    if not 1 <= count <= MAX_PACKED_CANDIDATES:
        raise ValueError("candidate count out of bounds")
    b, offset = _take(raw, offset, 20)
    asset = "0x" + b.hex()
    b, offset = _take(raw, offset, 32)
    amount = int.from_bytes(b, "big")
    b, offset = _take(raw, offset, 8)
    target_block = int.from_bytes(b, "big")
    b, offset = _take(raw, offset, 8)
    deadline = int.from_bytes(b, "big")

    candidates: list[PackedCandidate] = []
    for _ in range(count):
        b, offset = _take(raw, offset, 32)
        cid = "0x" + b.hex()
        b, offset = _take(raw, offset, 32)
        min_profit = int.from_bytes(b, "big")
        b, offset = _take(raw, offset, 1)
        leg_count = b[0]
        if not 2 <= leg_count <= MAX_PACKED_LEGS:
            raise ValueError("leg count out of bounds")
        legs: list[PackedLeg] = []
        for _ in range(leg_count):
            b, offset = _take(raw, offset, 20)
            adapter = "0x" + b.hex()
            b, offset = _take(raw, offset, 20)
            token_in = "0x" + b.hex()
            b, offset = _take(raw, offset, 20)
            token_out = "0x" + b.hex()
            b, offset = _take(raw, offset, 32)
            min_out = int.from_bytes(b, "big")
            b, offset = _take(raw, offset, 2)
            data_len = int.from_bytes(b, "big")
            if data_len > MAX_PACKED_LEG_DATA:
                raise ValueError("leg data too large")
            data, offset = _take(raw, offset, data_len)
            legs.append(PackedLeg(adapter, token_in, token_out, min_out, data))
        candidates.append(PackedCandidate(cid, min_profit, tuple(legs)))

    if offset != len(raw):
        raise ValueError("trailing packed bytes")
    return PackedBatch(asset, amount, target_block, deadline, tuple(candidates))


def select_batch_candidates(
    rows: Iterable[tuple[float, PackedCandidate]],
    *,
    max_candidates: int = MAX_PACKED_CANDIDATES,
) -> tuple[PackedCandidate, ...]:
    """Deterministically rank by caller-supplied EV, then candidate id for stable ties."""
    if not 1 <= max_candidates <= MAX_PACKED_CANDIDATES:
        raise ValueError("max_candidates out of bounds")
    ranked = sorted(rows, key=lambda x: (-x[0], x[1].candidate_id.lower()))
    return tuple(candidate for _, candidate in ranked[:max_candidates])
