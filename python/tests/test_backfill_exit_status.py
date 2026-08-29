"""A backfill that did not reach head must not report success."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_uniswap_pools.py"
_HEAD = 2_000_000


def _load():
    spec = importlib.util.spec_from_file_location("backfill_uniswap_pools", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"backfill dependencies unavailable: {exc}")
    return module


class _FakeEth:
    @property
    async def block_number(self):
        return _HEAD

    @staticmethod
    async def get_logs(_params):
        return []


class _FakeWeb3:
    eth = _FakeEth()

    def __init__(self, *_a, **_kw):
        pass

    @staticmethod
    def to_checksum_address(value):
        return value


class _FakeDiscovery:
    """Advances the uniswap cursor by `step` per pass; 0 means permanently stalled."""

    def __init__(self, step: int, start: int, blockers: dict | None = None):
        self.step = step
        self._uniswap_cursor = start
        self._blockers = blockers or {}

    def code_missing_blockers(self):
        return dict(self._blockers)

    async def verify_endpoint_consistency(self, *, force=False):
        return {}

    async def restore(self):
        return {"restored_pools": 0}

    async def discover_uniswap(self, _stats, latest):
        self._uniswap_cursor = min(self._uniswap_cursor + self.step, latest + 1)


async def _no_sleep(_seconds):
    """Skip the 2s stall backoff so the retry budget is exercised instantly."""
    return None


def _run(monkeypatch, mod, step: int, start: int = 1_371_680, max_stalled: int = 3,
         blockers: dict | None = None) -> int:
    monkeypatch.setattr(mod, "AsyncWeb3", _FakeWeb3)
    monkeypatch.setattr(mod, "AsyncHTTPProvider", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "PostgresPoolRegistry", lambda *a, **kw: object())
    monkeypatch.setattr(mod, "BasePoolDiscovery", lambda *a, **kw: _FakeDiscovery(step, start, blockers))
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(sys, "argv", [
        "backfill", "--rpc", "http://rpc.invalid", "--from-block", str(start),
        "--max-stalled-passes", str(max_stalled),
    ])
    return asyncio.run(mod.main())


def test_stalled_cursor_exits_non_zero_instead_of_reporting_done(monkeypatch, capsys):
    """Breaking out of the stall loop used to fall through to `print("done"); return 0`."""
    mod = _load()
    code = _run(monkeypatch, mod, step=0)
    assert code == 1
    err = capsys.readouterr().err
    assert "INCOMPLETE" in err
    assert "blocks short of head" in err


def test_incomplete_message_names_how_far_short_the_cursor_stopped(monkeypatch, capsys):
    """"done" told the operator nothing; the distance to head is what decides a re-run."""
    mod = _load()
    start = _HEAD - 5
    assert _run(monkeypatch, mod, step=0, start=start, max_stalled=2) == 1
    err = capsys.readouterr().err
    assert f"{start:,}" in err
    assert "5 blocks short of head" in err or "6 blocks short of head" in err


def test_partial_progress_that_still_reaches_head_succeeds(monkeypatch, capsys):
    """A scan that advances a chunk at a time is complete once the cursor passes head."""
    mod = _load()
    assert _run(monkeypatch, mod, step=1, start=_HEAD - 5, max_stalled=100) == 0
    assert "done -- cursor reached head" in capsys.readouterr().out


def test_reaching_head_reports_done_and_exits_zero(monkeypatch, capsys):
    mod = _load()
    code = _run(monkeypatch, mod, step=10**9)
    assert code == 0
    out = capsys.readouterr().out
    assert "done -- cursor reached head" in out
    assert "INCOMPLETE" not in out


def test_code_missing_blocker_is_reported_as_the_cause(monkeypatch, capsys):
    mod = _load()
    code = _run(monkeypatch, mod, step=0, max_stalled=100,
                blockers={"uniswap-v3": "blocks 100-200: 1 announced pool(s) with no code"})
    assert code == 1
    err = capsys.readouterr().err
    assert "blocked on an unverifiable pool" in err
    assert "no code" in err
    assert "INCOMPLETE" in err
