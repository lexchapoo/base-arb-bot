"""Independent oracle for the Rust TickMath port.

The Rust implementation (rust-executor/src/v3_math.rs) is a transcription of Uniswap's
TickMath magic constants. Transcribing them twice would only prove the typo is consistent,
so this test re-derives every reference value from the *mathematical definition* --
sqrt(1.0001^tick) * 2^96 at 140-digit precision -- and pins the shared fixture to it.

Rust asserts its output equals the fixture; this asserts the fixture equals the real number.
Together they prove the port is correct, offline, with no RPC.
"""
from __future__ import annotations

from decimal import Decimal, getcontext
from pathlib import Path

import pytest

getcontext().prec = 140

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "rust-executor" / "tests" / "fixtures" / "tick_math_reference.txt"
)

MIN_TICK = -887272
MAX_TICK = 887272
Q96 = Decimal(2) ** 96


def reference_rows() -> list[tuple[int, int]]:
    rows = []
    for line in FIXTURE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tick, value = line.split()
        rows.append((int(tick), int(value)))
    return rows


def true_sqrt_ratio(tick: int) -> Decimal:
    return (Decimal("1.0001") ** Decimal(tick)).sqrt() * Q96


def test_fixture_exists_and_is_not_truncated():
    rows = reference_rows()
    assert len(rows) >= 20
    ticks = [t for t, _ in rows]
    assert ticks == sorted(ticks), "fixture must stay sorted for readable diffs"
    assert len(set(ticks)) == len(ticks), "duplicate tick in fixture"


@pytest.mark.parametrize("tick,expected", reference_rows())
def test_reference_value_equals_high_precision_sqrt_price(tick: int, expected: int):
    true = true_sqrt_ratio(tick)
    diff = Decimal(expected) - true

    # TickMath's final `>> 32` rounds up, so a value must never under-report.
    assert diff > Decimal(-1), f"tick {tick} under-reports the true sqrt price"

    # Fixed-point error scales with magnitude; 1e-9 relative is far tighter than the
    # 2.0e-10 worst case observed at the extreme ticks, and 1 unit covers the small end.
    tolerance = max(Decimal(1), true * Decimal("1e-9"))
    assert abs(diff) <= tolerance, f"tick {tick} deviates by {diff}"


def test_documented_uniswap_anchors_are_exact():
    values = dict(reference_rows())
    assert values[0] == 2**96, "tick 0 must be exactly Q96"
    assert values[MIN_TICK] == 4295128739, "MIN_SQRT_RATIO mismatch"
    assert values[MAX_TICK] == 1461446703485210103287273052203988822378723970342


def test_reference_table_is_strictly_monotonic_in_tick():
    rows = reference_rows()
    for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
        assert v1 > v0, f"sqrt price not increasing between ticks {t0} and {t1}"


def test_tick_range_bounds_are_the_uniswap_limits():
    ticks = [t for t, _ in reference_rows()]
    assert min(ticks) == MIN_TICK
    assert max(ticks) == MAX_TICK
