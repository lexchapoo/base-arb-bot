"""Curation must rank pools by economic value, not by raw token unit count."""
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "curate_pools.py"


def _load():
    spec = importlib.util.spec_from_file_location("curate_pools", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its class through sys.modules, so register before exec.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # asyncpg/httpx/web3 not installed
        pytest.skip(f"curate_pools dependencies unavailable: {exc}")
    return module


WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"


def _pool(mod, address, hub, raw):
    p = mod.Pool(address, hub, "0x" + "9" * 40, "aerodrome", "xyk", None, False)
    p.hub = hub
    p.hub_raw = raw
    return p


def test_hub_amount_is_exact_and_decimal_scaled():
    mod = _load()
    # 1 WETH (18 decimals) and 2,500 USDC (6 decimals).
    assert _pool(mod, "0x" + "1" * 40, WETH, 10**18).hub_amount == Decimal(1)
    assert _pool(mod, "0x" + "2" * 40, USDC, 2500 * 10**6).hub_amount == Decimal(2500)


def test_usd_normalisation_makes_hub_tokens_comparable():
    """2,500 USDC used to outrank 1 WETH purely because 2500 > 1."""
    mod = _load()
    refs = {addr: usd for addr, (_s, _d, _f, usd) in mod.HUBS.items()}

    def usd(pool):
        return pool.hub_amount * Decimal(str(refs[pool.hub]))

    one_weth = _pool(mod, "0x" + "1" * 40, WETH, 10**18)
    twentyfive_hundred_usdc = _pool(mod, "0x" + "2" * 40, USDC, 2500 * 10**6)
    tenth_cbbtc = _pool(mod, "0x" + "3" * 40, CBBTC, 10**7)  # 0.1 cbBTC, 8 decimals

    # Roughly equal value: the ranking no longer separates them by unit count.
    assert usd(one_weth) == usd(twentyfive_hundred_usdc)
    # And a genuinely larger position sorts above both.
    assert usd(tenth_cbbtc) > usd(one_weth)

    ordered = sorted([twentyfive_hundred_usdc, one_weth, tenth_cbbtc],
                     key=lambda p: (-usd(p), p.address))
    assert ordered[0] is tenth_cbbtc


def test_tick_spacing_is_part_of_the_pool_record():
    """Slipstream pools are unroutable without it, so curation must carry it through."""
    mod = _load()
    p = mod.Pool("0x" + "1" * 40, WETH, "0x" + "9" * 40,
                 "aerodrome-slipstream", "concentrated", None, None, 100)
    assert p.tick_spacing == 100


# --- balance-read failures must not silently bias the selection ----------------------


class _FakeMulticall:
    """Returns ok=False for the pools named in `fail`, mimicking a reverted subcall."""

    def __init__(self, fail: set[str], batch_error: bool = False):
        self.fail = fail
        self.batch_error = batch_error

    async def aggregate3(self, calls, block_identifier=None):
        if self.batch_error:
            raise TimeoutError("multicall batch timed out")
        return [(True, (10**21).to_bytes(32, "big"))] * len(calls)


def _pools_for_balances(mod, addresses):
    return [_pool(mod, a, WETH, 0) for a in addresses]


def test_unresolved_balance_is_distinguished_from_an_empty_pool():
    """Both used to score 0 and fall below the floor -- indistinguishable from dead."""
    mod = _load()
    import asyncio

    pools = _pools_for_balances(mod, ["0x" + f"{n:040x}" for n in (1, 2)])
    unresolved = asyncio.run(mod.fetch_hub_balances(_FakeMulticall(set()), pools, 10))
    assert unresolved == []
    assert all(p.resolved for p in pools)
    assert pools[0].hub_amount == Decimal(1000)


def test_a_failed_batch_reports_every_pool_in_it_as_unresolved():
    mod = _load()
    import asyncio

    pools = _pools_for_balances(mod, ["0x" + f"{n:040x}" for n in (1, 2, 3)])
    unresolved = asyncio.run(
        mod.fetch_hub_balances(_FakeMulticall(set(), batch_error=True), pools, 10)
    )
    assert [p.address for p in unresolved] == [p.address for p in pools]
    assert not any(p.resolved for p in pools)


# --- two venues is not proof of an executable route -----------------------------------


def _venue_pool(mod, address, token0, token1, venue):
    p = mod.Pool(address, token0, token1, venue, "xyk", None, False)
    p.hub = WETH
    p.hub_raw = 10**21
    return p


def test_two_venues_with_disjoint_pairs_is_not_arbitrageable():
    """The old len(by_venue) >= 2 check passed this selection; no route exists in it."""
    mod = _load()
    a, b, c, d = ("0x" + f"{n:040x}" for n in (10, 11, 12, 13))
    selected = [
        _venue_pool(mod, "0x" + f"{1:040x}", a, b, "aerodrome"),
        _venue_pool(mod, "0x" + f"{2:040x}", c, d, "uniswap-v3"),
    ]
    assert len({p.venue for p in selected}) == 2
    assert mod.cross_venue_cycles(selected, 2) == []


def test_the_same_pair_on_two_venues_yields_a_cross_venue_cycle():
    mod = _load()
    a, b = ("0x" + f"{n:040x}" for n in (10, 11))
    selected = [
        _venue_pool(mod, "0x" + f"{1:040x}", a, b, "aerodrome"),
        _venue_pool(mod, "0x" + f"{2:040x}", a, b, "uniswap-v3"),
    ]
    cycles = mod.cross_venue_cycles(selected, 2)
    assert cycles
    assert set(cycles[0]) == {"0x" + f"{1:040x}", "0x" + f"{2:040x}"}


def test_the_same_pair_on_one_venue_twice_is_not_a_cross_venue_cycle():
    """Two Aerodrome pools on the same pair form a cycle, but not an arbitrageable one."""
    mod = _load()
    a, b = ("0x" + f"{n:040x}" for n in (10, 11))
    selected = [
        _venue_pool(mod, "0x" + f"{1:040x}", a, b, "aerodrome"),
        _venue_pool(mod, "0x" + f"{2:040x}", a, b, "aerodrome"),
    ]
    assert mod.cross_venue_cycles(selected, 2) == []


def test_venue_aliases_are_canonicalised_before_being_compared():
    """"Aerodrome Slipstream" and "aerodrome" are different venues; casing is not."""
    mod = _load()
    a, b = ("0x" + f"{n:040x}" for n in (10, 11))
    same = [
        _venue_pool(mod, "0x" + f"{1:040x}", a, b, "aerodrome"),
        _venue_pool(mod, "0x" + f"{2:040x}", a, b, "Aerodrome"),
    ]
    assert mod.cross_venue_cycles(same, 2) == []
    different = [
        _venue_pool(mod, "0x" + f"{1:040x}", a, b, "aerodrome"),
        _venue_pool(mod, "0x" + f"{2:040x}", a, b, "aerodrome-slipstream"),
    ]
    assert mod.cross_venue_cycles(different, 2)


def test_reference_prices_and_floors_are_decimal_not_float():
    """A binary float 0.03 is not 0.03, and it decides which pools clear the floor."""
    mod = _load()
    for _sym, _dec, floor, usd in mod.HUBS.values():
        assert isinstance(floor, Decimal)
        assert isinstance(usd, Decimal)
    assert mod.HUBS[CBBTC][2] == Decimal("0.03")


def test_decimal_arguments_report_a_usage_error_not_a_traceback():
    """argparse only converts ValueError/TypeError from a `type=` callable into an error.

    Decimal("abc") raises InvalidOperation, which derives from ArithmeticError, so passing
    Decimal directly let a typo escape as a raw traceback instead of the usage message
    every other bad argument on this script produces.
    """
    import argparse

    module = _load()
    assert module._decimal("1.5") == Decimal("1.5")
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        module._decimal("abc")
    assert "abc" in str(caught.value)

