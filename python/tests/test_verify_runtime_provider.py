"""Preflight must catch a Morpho misconfiguration before it blocks every route at runtime.

The executor and the finalizer both already refuse to borrow from a Morpho they do not have,
so nothing unsafe happens without these checks -- but the failure surfaces one route at a time,
as blockers, on a run that otherwise looks healthy. This moves it to startup, where it is one
line of output instead of a silent zero-fill shadow report.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts_verify_runtime.py"

EXECUTOR = "0x" + "11" * 20
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
OTHER_MORPHO = "0x" + "22" * 20


def _load():
    spec = importlib.util.spec_from_file_location("verify_runtime", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # httpx missing
        pytest.skip(f"verify_runtime dependencies unavailable: {exc}")
    return module


def _env(monkeypatch, module, **overrides):
    """Every address the script demands, all pointing at contracts that exist."""
    monkeypatch.setattr(module, "RPC", "http://stub", raising=False)
    monkeypatch.setattr(module, "EXPECTED", 8453, raising=False)
    base = {name: EXECUTOR for name in module.NAMES}
    base["EXECUTOR_ADDRESS"] = EXECUTOR
    base.update(overrides)
    for key in ("FLASH_PROVIDER", "MORPHO_ADDRESS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _stub_rpc(module, monkeypatch, *, executor_morpho=MORPHO, call_raises=False):
    def rpc(method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_getCode":
            return "0x60006000"
        if method == "eth_call":
            if call_raises:
                raise RuntimeError("execution reverted")
            if executor_morpho is None:
                return "0x" + "00" * 32
            return "0x" + "00" * 12 + executor_morpho[2:]
        raise AssertionError(f"unexpected rpc {method}")

    monkeypatch.setattr(module, "rpc", rpc)


def test_aave_is_the_default_and_needs_no_morpho(monkeypatch):
    module = _load()
    _env(monkeypatch, module)
    _stub_rpc(module, monkeypatch)
    assert module.main() == 0


def test_morpho_provider_requires_the_address(monkeypatch):
    """Without it every route blocks on morpho_address_not_configured; say so at startup."""
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="morpho")
    _stub_rpc(module, monkeypatch)
    assert module.main() == 1


def test_morpho_provider_passes_when_env_and_executor_agree(monkeypatch):
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="morpho", MORPHO_ADDRESS=MORPHO)
    _stub_rpc(module, monkeypatch, executor_morpho=MORPHO)
    assert module.main() == 0


def test_a_drifted_executor_morpho_is_caught(monkeypatch):
    """The env var and the deployed executor naming different Morphos is the dangerous case.

    Sizing and profitability get priced against liquidity the executor will never borrow from.
    """
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="morpho", MORPHO_ADDRESS=MORPHO)
    _stub_rpc(module, monkeypatch, executor_morpho=OTHER_MORPHO)
    assert module.main() == 5


def test_an_executor_deployed_without_morpho_is_caught(monkeypatch):
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="morpho", MORPHO_ADDRESS=MORPHO)
    _stub_rpc(module, monkeypatch, executor_morpho=None)
    assert module.main() == 5


def test_an_executor_predating_the_upgrade_is_caught(monkeypatch):
    """MORPHO() does not exist on the currently deployed executor, so the call reverts."""
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="morpho", MORPHO_ADDRESS=MORPHO)
    _stub_rpc(module, monkeypatch, call_raises=True)
    assert module.main() == 5


def test_an_unknown_provider_is_rejected(monkeypatch):
    module = _load()
    _env(monkeypatch, module, FLASH_PROVIDER="balancer")
    _stub_rpc(module, monkeypatch)
    assert module.main() == 4


def test_an_unused_morpho_address_is_still_verified(monkeypatch):
    """A typo parked in MORPHO_ADDRESS is a trap armed for whenever the switch is flipped."""
    module = _load()
    _env(monkeypatch, module, MORPHO_ADDRESS="0xnot-an-address")
    _stub_rpc(module, monkeypatch)
    assert module.main() == 1


def test_an_unused_but_valid_morpho_address_does_not_block_the_aave_path(monkeypatch):
    module = _load()
    _env(monkeypatch, module, MORPHO_ADDRESS=MORPHO)
    # No eth_call is made on the aave path: the executor's MORPHO() is irrelevant there.
    _stub_rpc(module, monkeypatch, call_raises=True)
    assert module.main() == 0
