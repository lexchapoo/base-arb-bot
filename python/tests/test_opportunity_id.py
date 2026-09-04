"""The submission identity must satisfy every consumer that stores or validates it.

`_opportunity_id` is written to `opportunity_telemetry.opportunity_id` (VARCHAR(96)) and
forwarded verbatim by the Rust boundary to the external signer, whose execution policy accepts
only a 32-byte hex `route_id`. A longer, structured identity silently drops the telemetry row
(the insert is wrapped in `except: pass`) and makes every live signing request fail policy.
"""
from arb_bot.api import _opportunity_id
from arb_bot.db.models import OpportunityTelemetryRecord
from arb_bot.models import PendingLogTrigger

POOL = "0x" + "11" * 20
BATCH_HASH = "0x" + "ab" * 32
STATE_VERSION = "0x" + "cd" * 32


class _Plan:
    def __init__(self, batch_hash):
        self.batch_hash = batch_hash


def _trigger(**overrides):
    values = {
        "pool_address": POOL,
        "observed_at_unix_ms": 1_700_000_000_000,
        "state_version": STATE_VERSION,
        "state_sequence": 7,
    }
    values.update(overrides)
    return PendingLogTrigger(**values)


def _column_length() -> int:
    return OpportunityTelemetryRecord.__table__.c.opportunity_id.type.length


def test_opportunity_id_is_a_32_byte_hex_commitment():
    for plan in (_Plan(BATCH_HASH), _Plan(None), None):
        value = _opportunity_id(_trigger(), plan)
        assert value.startswith("0x")
        # Exactly the shape signer_gateway.policy.validate_execution enforces on `route_id`.
        assert len(value) == 66
        int(value, 16)
        assert len(value) <= _column_length()


def test_opportunity_id_separates_batches_and_state_fingerprints():
    a = _opportunity_id(_trigger(), _Plan(BATCH_HASH))
    same_batch_new_state = _opportunity_id(_trigger(state_version="0x" + "ef" * 32), _Plan(BATCH_HASH))
    new_batch_same_state = _opportunity_id(_trigger(), _Plan("0x" + "cc" * 32))
    assert len({a, same_batch_new_state, new_batch_same_state}) == 3
    # Stable for identical inputs, so the Rust reconcile echo still resolves the same row.
    assert a == _opportunity_id(_trigger(), _Plan(BATCH_HASH))


def test_opportunity_id_without_state_version_falls_back_to_sequence():
    without_version = _opportunity_id(_trigger(state_version=None), _Plan(BATCH_HASH))
    other_sequence = _opportunity_id(_trigger(state_version=None, state_sequence=8), _Plan(BATCH_HASH))
    assert len(without_version) == 66
    assert without_version != other_sequence
