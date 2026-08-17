import pytest

from arb_bot.packed_batch import (
    PackedBatch,
    PackedCandidate,
    PackedLeg,
    candidate_id,
    decode_packed_batch,
    encode_packed_batch,
    select_batch_candidates,
)

A = "0x" + "11" * 20
B = "0x" + "22" * 20
C = "0x" + "33" * 20
D = "0x" + "44" * 20


def leg(adapter, token_in, token_out, min_out, data=b""):
    return PackedLeg(adapter, token_in, token_out, min_out, data)


def candidate(name: str, min_profit: int):
    return PackedCandidate(
        candidate_id(name),
        min_profit,
        (
            leg(C, A, B, 123, b"\x01\x02"),
            leg(D, B, A, 110, b""),
        ),
    )


def test_round_trip_is_exact():
    batch = PackedBatch(A, 1000, 123456, 999999999, (candidate("one", 10), candidate("two", 9)))
    raw = encode_packed_batch(batch)
    assert decode_packed_batch(raw) == batch
    assert encode_packed_batch(decode_packed_batch(raw)) == raw


def test_ranking_is_ev_first_and_stable():
    c1, c2, c3 = candidate("a", 1), candidate("b", 1), candidate("c", 1)
    selected = select_batch_candidates([(2.0, c2), (3.0, c3), (2.0, c1)], max_candidates=2)
    assert selected[0] == c3
    assert selected[1].candidate_id == min(c1.candidate_id, c2.candidate_id)


def test_rejects_trailing_data():
    batch = PackedBatch(A, 1000, 1, 2, (candidate("one", 1),))
    with pytest.raises(ValueError, match="trailing"):
        decode_packed_batch(encode_packed_batch(batch) + b"\x00")
