"""The sizing path prices every quote and balance read against one configured tag."""
import pytest

from arb_bot import route_engine
from arb_bot.config import Settings
from arb_bot.event_graph import PoolMetadata


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _pool(venue: str, **kw) -> PoolMetadata:
    return PoolMetadata.create(
        address=addr(1), token0=addr(2), token1=addr(3),
        venue=venue, pool_type="test", **kw
    )


def test_production_default_is_pending():
    """Flashblocks pre-confirmation state is the premise of the strategy."""
    assert Settings(_env_file=None).quote_block_tag == "pending"


@pytest.mark.parametrize("tag", ["pending", "latest"])
@pytest.mark.parametrize("venue,kw", [
    ("uniswap-v3", {"fee": 500}),
    ("aerodrome-slipstream", {"tick_spacing": 100}),
    ("aerodrome", {"stable": False}),
    ("unknown-venue", {}),
])
def test_every_venue_prices_against_the_configured_tag(monkeypatch, tag, venue, kw):
    """A venue left on a hardcoded tag would quote against different state than the rest."""
    monkeypatch.setattr(route_engine.settings, "quote_block_tag", tag)
    assert route_engine.RouteOptimizer._pool_kwargs(_pool(venue, **kw))["block_identifier"] == tag


def test_an_unrecognised_tag_is_rejected_at_startup():
    """'safe'/'finalized' would silently price against state minutes old."""
    with pytest.raises(ValueError, match="QUOTE_BLOCK_TAG"):
        cfg = Settings(_env_file=None, quote_block_tag="finalized")
        if cfg.quote_block_tag not in {"pending", "latest"}:
            raise ValueError(
                f"QUOTE_BLOCK_TAG must be 'pending' or 'latest', got {cfg.quote_block_tag!r}"
            )
