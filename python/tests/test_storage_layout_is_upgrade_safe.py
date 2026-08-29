"""The UUPS proxy's storage layout may only ever grow at the end.

The proxy holds the state; the implementation only describes how to read it. A variable
inserted anywhere before the end shifts every slot after it, so an upgrade would make `owner`
read whatever `pendingOwner` held -- zero on a proxy that never started a handover -- and brick
the contract beyond recovery, taking the adapter and token allowlists with it. There is no
recovery path: no unpause, no sweep, no further upgrade.

This is not hypothetical. `MORPHO` was first added directly after `POOL`, which put it at slot
1 where `owner` lives and shifted all fourteen variables behind it. Nothing in the test suite
noticed, because every unit test deploys a fresh contract where the layout is self-consistent.
Only a diff against the *deployed* layout catches it, which is what this does.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "contracts"
_GOLDEN = _CONTRACTS / "storage-layout.deployed.json"
_CONTRACT = "BaseArbExecutorUpgradeable"


def _current_layout():
    if shutil.which("forge") is None:
        pytest.skip("forge not installed")
    if not _GOLDEN.exists():
        pytest.fail(f"missing baseline {_GOLDEN}")
    result = subprocess.run(
        ["forge", "inspect", _CONTRACT, "storage-layout", "--json"],
        cwd=_CONTRACTS, capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        # A stale artifact cache reports the layout as missing rather than failing the build,
        # and a plain rebuild will not replace it -- foundry keeps the cached artifact even
        # once extra_output asks for a layout. Only a clean rebuild does, which is acceptable
        # here because this path runs solely after inspect has already failed.
        subprocess.run(["forge", "clean"], cwd=_CONTRACTS, capture_output=True, timeout=120)
        subprocess.run(["forge", "build"], cwd=_CONTRACTS, capture_output=True, timeout=600)
        result = subprocess.run(
            ["forge", "inspect", _CONTRACT, "storage-layout", "--json"],
            cwd=_CONTRACTS, capture_output=True, text=True, timeout=300,
        )
    if result.returncode != 0:
        # Deliberately a failure, not a skip. Skipping here once let a genuinely broken layout
        # report as four green tests: forge had a stale cache, inspect failed, and the guard
        # quietly stood down at exactly the moment it was needed. Only a missing forge binary
        # is grounds for skipping.
        pytest.fail(f"forge inspect failed after a rebuild: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)["storage"]


def test_every_deployed_variable_keeps_its_slot():
    """Position, not just presence: a variable that moved is as fatal as one that vanished."""
    golden = json.loads(_GOLDEN.read_text())["entries"]
    current = {entry["label"]: entry for entry in _current_layout()}

    moved = []
    for expected in golden:
        actual = current.get(expected["label"])
        if actual is None:
            moved.append(f"{expected['label']}: removed (was slot {expected['slot']})")
            continue
        if int(actual["slot"]) != expected["slot"] or int(actual["offset"]) != expected["offset"]:
            moved.append(
                f"{expected['label']}: slot {expected['slot']}.{expected['offset']}"
                f" -> {actual['slot']}.{actual['offset']}"
            )
    assert not moved, (
        "storage layout diverged from the deployed proxy; upgrading would corrupt state:\n  "
        + "\n  ".join(moved)
    )


def test_new_state_is_appended_past_the_deployed_tail():
    """Anything new must live at or after the last deployed slot, never woven in among it."""
    golden = json.loads(_GOLDEN.read_text())["entries"]
    known = {entry["label"] for entry in golden}
    last_deployed_slot = max(entry["slot"] for entry in golden)

    misplaced = [
        f"{entry['label']} at slot {entry['slot']}"
        for entry in _current_layout()
        if entry["label"] not in known and int(entry["slot"]) < last_deployed_slot
    ]
    assert not misplaced, (
        "new storage inserted before the end of the deployed layout: " + ", ".join(misplaced)
    )


def test_the_baseline_matches_the_contract_it_claims_to_describe():
    """Guards the guard: a baseline regenerated from the current build proves nothing.

    Every label in the baseline must still exist, so deleting a variable and refreshing the
    file cannot quietly make the other two tests vacuous.
    """
    golden = json.loads(_GOLDEN.read_text())["entries"]
    labels = {entry["label"] for entry in _current_layout()}
    missing = [entry["label"] for entry in golden if entry["label"] not in labels]
    assert not missing, f"baseline names variables the contract no longer has: {missing}"


def test_morpho_is_appended_and_therefore_zero_until_set():
    """Consequence worth stating: an upgraded proxy reads MORPHO as zero.

    initialize() has already run and cannot run again, so the value cannot be back-filled by
    re-initialising -- setMorpho() is the only way in, and until it is called Morpho routes
    revert with ProviderNotConfigured rather than falling back to Aave.
    """
    golden = json.loads(_GOLDEN.read_text())["entries"]
    last_deployed_slot = max(entry["slot"] for entry in golden)
    morpho = next((e for e in _current_layout() if e["label"] == "MORPHO"), None)
    assert morpho is not None, "MORPHO missing from the upgradeable contract"
    assert int(morpho["slot"]) > last_deployed_slot, (
        f"MORPHO at slot {morpho['slot']} overlaps deployed state ending at {last_deployed_slot}"
    )
