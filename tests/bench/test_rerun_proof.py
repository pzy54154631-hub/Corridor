"""Synthetic proof bundles reject omitted evidence without selecting by outcomes."""

from hashlib import sha256
from pathlib import Path

import pytest

from corridor.bench.bank_cases import write_json
from corridor.bench.rerun_proof import create_bundle, verify_bundle
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

CODE = {"src/corridor/fixture.py": "1" * 64}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    root = tmp_path / "source"
    blocks = [
        {"block_id": identity, "experiment": experiment, "model": "synthetic", "configs": ["A5"]}
        for identity, experiment in (("a", "E1"), ("b", "E2"), ("c", "E4"))
    ]
    write_json(root / "plan.json", {"blocks": blocks})
    for identity in ("a", "b"):
        final_block(root, identity)
    # This interrupted E4 attempt has no finalized block and is not snapshot input.
    write_json(root / "episodes/c/A5/0/result.json", {"status": "unknown"})
    manifest = tmp_path / "rerun.json"
    write_json(
        manifest,
        {
            "schema": "zh-rerun-manifest-v2",
            "merge_rule": "whole_block_replacement",
            "source_plan": str(root / "plan.json"),
            "source_plan_sha256": digest(root / "plan.json"),
            "blocks": [
                {
                    **blocks[0],
                    "execution_kind": "rerun",
                    "source_result_sha256": digest(root / "blocks/a.json"),
                }
            ],
        },
    )
    return root, manifest


def final_block(root, identity):
    write_json(
        root / "blocks" / (identity + ".json"),
        {
            "block_id": identity,
            "rows": [{"config": "A5", "status": "valid_completed"}],
            "attempts": [{"config": "A5", "attempt": 0}],
        },
    )
    write_json(
        root / "episodes" / identity / "A5/0/result.json",
        {
            "status": "valid_completed",
        },
    )
    write_json(
        root / "episodes" / identity / "A5/0/recording/call.json",
        {
            "synthetic_recording": identity,
        },
    )


def evidence(tmp_path, root, *, name="snapshot", inconsistent=(), missing=()):
    snapshot = tmp_path / name
    snapshot.mkdir()
    block_ids = sorted(path.stem for path in (root / "blocks").glob("*.json"))
    files = [root / "plan.json"]
    for identity in block_ids:
        files.append(root / "blocks" / (identity + ".json"))
        files.extend(path for path in (root / "episodes" / identity).rglob("*") if path.is_file())
    hashes = {}
    for path in files:
        relative = path.relative_to(root)
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        hashes[relative.as_posix()] = digest(path)
    write_json(
        snapshot / "snapshot.json",
        {
            "schema": "freeze8-completed-source-snapshot-v1",
            "source": str(root),
            "source_plan_sha256": digest(root / "plan.json"),
            "source_selected_files_unchanged": True,
            "block_ids": block_ids,
            "blocks": len(block_ids),
            "files": len(hashes),
            "sha256": hashes,
        },
    )
    results = {
        name: value
        for name, value in hashes.items()
        if name.startswith("blocks/")
        or (
            name.startswith("episodes/")
            and len(Path(name).parts) == 5
            and name.endswith("/result.json")
        )
    }
    ids = sorted(
        name.removeprefix("episodes/").removesuffix("/result.json")
        for name in results
        if name.startswith("episodes/")
    )
    failed = sorted(inconsistent)
    proof = tmp_path / (name + "-proof.json")
    write_json(
        proof,
        {
            "schema": "zh-source-replay-proof-v1",
            "network_disabled": True,
            "source_unchanged": True,
            "code_unchanged": True,
            "source_plan_sha256": digest(root / "plan.json"),
            "source_result_sha256": results,
            "replay_code_sha256": CODE,
            "units": len(ids),
            "replayed": len(ids),
            "consistent": len(ids) - len(failed),
            "inconsistent": len(failed),
            "inconsistent_ids": failed,
            "unit_results": [
                {"id": identity, "consistent": identity not in failed} for identity in ids
            ],
            "missing_source_attempts": sorted(missing),
            "passed": not failed and not missing,
        },
    )
    return snapshot, proof


def test_bundle_allows_selected_differences_and_ignores_unfinalized_e4(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root, inconsistent=("a/A5/0",))
    output = tmp_path / "bundle.json"
    identity = create_bundle([manifest], [snapshot], [proof], output, replay_code_sha256=CODE)
    assert identity["sources"][0]["completed_blocks"] == 2
    assert identity["sources"][0]["retained_completed_blocks"] == 1
    checked = verify_bundle(
        output,
        expected_sha256=identity["bundle_sha256"],
        manifest_paths=[manifest],
        source_paths=[root],
        replay_code_sha256=CODE,
    )
    assert checked == identity
    with pytest.raises(ValueError, match="fresh"):
        create_bundle([manifest], [snapshot], [proof], output)


@pytest.mark.parametrize("kind", ["difference", "missing"])
def test_retained_block_difference_or_missing_attempt_closes_gate(tmp_path, kind):
    root, manifest = fixture(tmp_path)
    if kind == "difference":
        snapshot, proof = evidence(tmp_path, root, inconsistent=("b/A5/0",))
    else:
        (root / "episodes/b/A5/0/result.json").unlink()
        snapshot, proof = evidence(
            tmp_path,
            root,
            missing=("b/A5/0", "b/A5/missing-observation"),
        )
    with pytest.raises(ValueError, match="outside reruns"):
        create_bundle([manifest], [snapshot], [proof], tmp_path / "bundle.json")


def test_selected_missing_recording_is_explicitly_bound(tmp_path):
    root, manifest = fixture(tmp_path)
    (root / "episodes/a/A5/0/result.json").unlink()
    snapshot, proof = evidence(
        tmp_path,
        root,
        missing=("a/A5/0", "a/A5/missing-observation"),
    )
    identity = create_bundle([manifest], [snapshot], [proof], tmp_path / "bundle.json")
    assert identity["verified"] is True


@pytest.mark.parametrize("damage", ["omitted_unit", "duplicate_unit", "missing_list", "counter"])
def test_proof_cannot_omit_or_duplicate_observations(tmp_path, damage):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    value = loads_exact(proof.read_bytes())
    if damage == "omitted_unit":
        value["unit_results"] = value["unit_results"][:1]
        value["units"] = value["replayed"] = value["consistent"] = 1
    elif damage == "duplicate_unit":
        value["unit_results"].append(value["unit_results"][0])
    elif damage == "missing_list":
        value["missing_source_attempts"] = ["b/A5/missing-observation"]
    else:
        value["consistent"] = 0
    write_json(proof, value)
    with pytest.raises(ValueError, match="coverage or counters"):
        create_bundle([manifest], [snapshot], [proof], tmp_path / "bundle.json")


@pytest.mark.parametrize("damage", ["manifest", "proof", "snapshot", "source_recording", "bundle"])
def test_every_input_is_hash_bound_and_originals_are_not_modified(tmp_path, damage):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    output = tmp_path / "bundle.json"
    identity = create_bundle([manifest], [snapshot], [proof], output)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert verify_bundle(output)["verified"] is True
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    target = {
        "manifest": manifest,
        "proof": proof,
        "snapshot": snapshot / "snapshot.json",
        "source_recording": root / "episodes/b/A5/0/recording/call.json",
        "bundle": output,
    }[damage]
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash changed"):
        verify_bundle(output, expected_sha256=identity["bundle_sha256"])


def test_execution_checks_code_report_checks_frozen_bundle_and_evidence(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    output = tmp_path / "bundle.json"
    identity = create_bundle([manifest], [snapshot], [proof], output)
    with pytest.raises(ValueError, match="current frozen replay code"):
        verify_bundle(output, replay_code_sha256={"src/corridor/fixture.py": "2" * 64})
    assert verify_bundle(output, expected_sha256=identity["bundle_sha256"])["verified"]
    (root / "episodes/b/A5/0/result.json").write_bytes(jcs_bytes({"status": "changed"}))
    with pytest.raises(ValueError, match="evidence hash changed"):
        verify_bundle(output, expected_sha256=identity["bundle_sha256"])


def test_newly_finalized_e4_requires_additional_bound_proof(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    output = tmp_path / "bundle.json"
    create_bundle([manifest], [snapshot], [proof], output)
    final_block(root, "c")
    with pytest.raises(ValueError, match="coverage"):
        verify_bundle(output)
    incremental, incremental_proof = evidence(tmp_path, root, name="incremental")
    identity = create_bundle(
        [manifest],
        [snapshot, incremental],
        [proof, incremental_proof],
        tmp_path / "expanded-bundle.json",
        replay_code_sha256=CODE,
    )
    assert identity["sources"][0]["completed_blocks"] == 3


def test_unbound_manifest_and_uncovered_completed_file_are_rejected(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    output = tmp_path / "bundle.json"
    create_bundle([manifest], [snapshot], [proof], output)
    other = tmp_path / "unbound-manifest.json"
    other.write_bytes(manifest.read_bytes())
    with pytest.raises(ValueError, match="not bound"):
        verify_bundle(output, manifest_paths=[other])
    write_json(root / "episodes/b/A5/0/extra.json", {"new": "unproved"})
    with pytest.raises(ValueError, match="outside the bound snapshots"):
        verify_bundle(output)


def test_bundle_checks_union_of_all_manifests_for_the_source(tmp_path):
    root, first = fixture(tmp_path)
    second = tmp_path / "second-manifest.json"
    manifest = loads_exact(first.read_bytes())
    block = loads_exact((root / "plan.json").read_bytes())["blocks"][1]
    manifest["blocks"] = [
        {
            **block,
            "execution_kind": "rerun",
            "source_result_sha256": digest(root / "blocks/b.json"),
        }
    ]
    write_json(second, manifest)
    snapshot, proof = evidence(tmp_path, root, inconsistent=("a/A5/0", "b/A5/0"))
    output = tmp_path / "bundle.json"
    create_bundle([first, second], [snapshot], [proof], output)
    identity = verify_bundle(output, manifest_paths=[first])
    assert identity["sources"][0]["selected_rerun_blocks"] == 2
    assert identity["sources"][0]["retained_completed_blocks"] == 0


def test_incremental_proofs_cannot_change_frozen_replay_code(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    final_block(root, "c")
    added, added_proof = evidence(tmp_path, root, name="added")
    value = loads_exact(added_proof.read_bytes())
    value["replay_code_sha256"] = {"src/corridor/fixture.py": "2" * 64}
    write_json(added_proof, value)
    with pytest.raises(ValueError, match="different frozen code"):
        create_bundle([manifest], [snapshot, added], [proof, added_proof], tmp_path / "bundle.json")


def test_supplement_covers_later_e4_without_rebinding_frozen_run(tmp_path):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    original = tmp_path / "original-bundle.json"
    frozen = create_bundle([manifest], [snapshot], [proof], original)
    original_bytes = original.read_bytes()
    final_block(root, "c")
    extra, extra_proof = evidence(tmp_path, root, name="later-e4")
    supplement = tmp_path / "supplement.json"
    extended = create_bundle(
        [manifest],
        [snapshot, extra],
        [proof, extra_proof],
        supplement,
    )
    with pytest.raises(ValueError, match="coverage"):
        verify_bundle(original, expected_sha256=frozen["bundle_sha256"])
    result = verify_bundle(
        original,
        expected_sha256=frozen["bundle_sha256"],
        supplemental_path=supplement,
    )
    assert result["sources"][0]["completed_blocks"] == 3
    assert result["bundle_sha256"] == extended["bundle_sha256"]
    assert result["original_bundle_sha256"] == frozen["bundle_sha256"]
    assert result["original_bundle_path"] == str(original)
    assert original.read_bytes() == original_bytes
    with pytest.raises(ValueError, match="original frozen bundle hash"):
        verify_bundle(original, supplemental_path=supplement)
    with pytest.raises(ValueError, match="bundle hash changed"):
        verify_bundle(original, expected_sha256="0" * 64, supplemental_path=supplement)


@pytest.mark.parametrize("damage", ["remove_proof", "replace_proof", "change_selection"])
def test_supplement_cannot_replace_any_frozen_record(tmp_path, damage):
    root, manifest = fixture(tmp_path)
    snapshot, proof = evidence(tmp_path, root)
    original = tmp_path / "original-bundle.json"
    frozen = create_bundle([manifest], [snapshot], [proof], original)
    final_block(root, "c")
    extra, extra_proof = evidence(tmp_path, root, name="later-e4")
    supplement = tmp_path / "supplement.json"
    create_bundle([manifest], [snapshot, extra], [proof, extra_proof], supplement)
    value = loads_exact(supplement.read_bytes())
    if damage == "remove_proof":
        value["proofs"] = value["proofs"][1:]
    elif damage == "replace_proof":
        value["proofs"][0]["proof_sha256"] = "0" * 64
    else:
        value["manifests"][0]["sha256"] = "0" * 64
    write_json(supplement, value)
    with pytest.raises(ValueError, match="replaced an original proof record"):
        verify_bundle(
            original,
            expected_sha256=frozen["bundle_sha256"],
            supplemental_path=supplement,
        )
