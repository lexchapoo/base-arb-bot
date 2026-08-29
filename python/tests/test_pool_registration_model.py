"""Registration must carry every field the quoter needs to route the pool."""
import pytest
from pydantic import ValidationError

from arb_bot.models import PoolRegistration


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _base(**kw) -> dict:
    body = {"address": addr(1), "token0": addr(2), "token1": addr(3),
            "venue": "aerodrome-slipstream", "pool_type": "concentrated", "tick_spacing": 100}
    body.update(kw)
    return body


def test_slipstream_registration_round_trips_tick_spacing():
    assert PoolRegistration(**_base()).tick_spacing == 100


def test_slipstream_registration_without_tick_spacing_is_rejected():
    """Without it the route engine raises missing_tick_spacing and the pool is dead weight."""
    with pytest.raises(ValidationError, match="tick_spacing"):
        PoolRegistration(**_base(tick_spacing=None))


def test_slipstream_venue_aliases_are_canonicalised_before_the_check():
    with pytest.raises(ValidationError, match="tick_spacing"):
        PoolRegistration(**_base(venue="Aerodrome Slipstream", tick_spacing=None))


def test_uniswap_v3_registration_requires_fee():
    with pytest.raises(ValidationError, match="fee"):
        PoolRegistration(**_base(venue="uniswap-v3", tick_spacing=None))
    assert PoolRegistration(**_base(venue="uniswap-v3", tick_spacing=None, fee=500)).fee == 500


def test_aerodrome_registration_requires_the_stable_flag():
    with pytest.raises(ValidationError, match="stable"):
        PoolRegistration(**_base(venue="aerodrome", pool_type="xyk", tick_spacing=None))
    assert PoolRegistration(
        **_base(venue="aerodrome", pool_type="xyk", tick_spacing=None, stable=False)
    ).stable is False
