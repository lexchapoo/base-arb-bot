from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "scripts_prepare_deployment", ROOT / "scripts_prepare_deployment.py"
)
assert SPEC is not None and SPEC.loader is not None
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def test_create_address_matches_known_ethereum_vector() -> None:
    sender = "0x6aC7Ea33F8831eA9DCc53393AaA88B25a785DBF0"
    assert deployment.create_address(sender, 0) == "0xcd234A471b72ba2F1Ccf0A70FCABA648a5eeCD8d"


def test_configuration_calldata_has_expected_selector() -> None:
    calldata = deployment.call_data(
        "setAdapter(address,bool)",
        ["address", "bool"],
        ["0x0000000000000000000000000000000000000001", True],
    )
    assert calldata.startswith("0x" + deployment.keccak(text="setAdapter(address,bool)")[:4].hex())
    assert len(calldata) == 2 + (4 + 32 + 32) * 2
