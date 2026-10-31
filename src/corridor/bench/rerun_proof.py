"""Content-bound R13 source replay bundles; no model calls or outcome selection.

Only finalized block files select source coverage. Outcome values are never used
to choose reruns: manifests supply that selection, and attempt metadata is used
only to verify that a proof did not omit an executed unit.
"""

from hashlib import sha256
from pathlib import Path, PurePosixPath

from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

SCHEMA = "zh-rerun-proof-bundle-v1"
MANIFEST_SCHEMAS = {"zh-rerun-manifest-v1", "zh-rerun-manifest-v2"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        raise ValueError("R13 proof input is missing or unreadable") from None


def _document(path):
    value = loads_exact(_read(path))
    _require(isinstance(value, dict), "R13 proof input must be an object")
    return value


def _digest(path):
    return sha256(_read(path)).hexdigest()


def _safe_file(root, relative):
    _require(isinstance(relative, str) and "\\" not in relative, "Invalid proof relative path")
    parts = PurePosixPath(relative)
    _require(
        bool(parts.parts) and not parts.is_absolute() and ".." not in parts.parts,
        "Invalid proof relative path",
    )
    root = Path(root).resolve()
    path = root.joinpath(*parts.parts).resolve()
    _require(path.is_relative_to(root), "Proof path escaped its source")
    return path


def _blocks(plan):
    rows = plan.get("blocks")
    _require(isinstance(rows, list) and bool(rows), "Proof source plan has no blocks")
    result = {}
    for row in rows:
        _require(isinstance(row, dict), "Invalid source plan block")
        identity = row.get("block_id")
        _require(
            isinstance(identity, str)
            and bool(identity)
            and "/" not in identity
            and "\\" not in identity
            and identity not in {".", ".."}
            and identity not in result,
            "Invalid or duplicate source plan block ID",
        )
        configs = row.get("configs")
        _require(
            isinstance(configs, list)
            and bool(configs)
            and all(isinstance(config, str) and config for config in configs)
            and len(set(configs)) == len(configs),
            "Invalid source block configurations",
        )
        result[identity] = row
    return result


def _result_identity(relative):
    parts = PurePosixPath(relative).parts
    if len(parts) == 5 and parts[0] == "episodes" and parts[-1] == "result.json":
        return "/".join(parts[1:-1])
    return None


def _missing_attempts(snapshot, block_ids, plans, result_ids):
    """Check identity/status metadata only, never outcomes or model responses."""
    missing = set()
    for block_id in block_ids:
        report = _document(snapshot / "blocks" / (block_id + ".json"))
        rows = report.get("rows")
        _require(
            isinstance(rows, list)
            and all(isinstance(row, dict) for row in rows)
            and [row.get("config") for row in rows] == plans[block_id]["configs"]
            and all(isinstance(row.get("status"), str) for row in rows),
            "Replay snapshot has incomplete final block rows",
        )
        attempts = report.get("attempts", [])
        _require(isinstance(attempts, list), "Invalid source attempt metadata")
        seen = set()
        for attempt in attempts:
            _require(isinstance(attempt, dict), "Invalid source attempt metadata")
            config, number = attempt.get("config"), attempt.get("attempt")
            _require(
                config in plans[block_id]["configs"]
                and type(number) is int
                and number >= 0
                and (config, number) not in seen,
                "Invalid or duplicate source attempt identity",
            )
            seen.add((config, number))
            identity = f"{block_id}/{config}/{number}"
            if identity not in result_ids:
                missing.add(identity)
        for row in rows:
            if row["status"] in {"not_run", "invalid_case"}:
                continue
            prefix = f"{block_id}/{row['config']}/"
            if not any(identity.startswith(prefix) for identity in result_ids):
                missing.add(prefix + "missing-observation")
    return sorted(missing)


def _manifest_sources(records):
    sources, paths, hashes = {}, set(), set()
    for record in records:
        path = Path(record["path"]).resolve()
        _require(path not in paths, "Duplicate bound rerun manifest")
        paths.add(path)
        digest = _digest(path)
        _require(digest == record.get("sha256"), "Bound rerun manifest hash changed")
        hashes.add(digest)
        manifest = _document(path)
        _require(
            manifest.get("schema") in MANIFEST_SCHEMAS
            and manifest.get("merge_rule") == "whole_block_replacement",
            "Invalid bound rerun manifest",
        )
        plan_path = Path(manifest["source_plan"]).resolve()
        plan_digest = _digest(plan_path)
        _require(
            plan_digest == manifest.get("source_plan_sha256"),
            "Bound source plan hash changed",
        )
        plan_blocks = _blocks(_document(plan_path))
        entries = manifest.get("blocks")
        _require(
            isinstance(entries, list)
            and bool(entries)
            and all(isinstance(entry, dict) for entry in entries),
            "Invalid bound rerun selection",
        )
        ids = [entry.get("block_id") for entry in entries]
        _require(
            all(isinstance(identity, str) for identity in ids)
            and len(set(ids)) == len(ids)
            and [identity for identity in plan_blocks if identity in set(ids)] == ids,
            "Bound rerun selection order or identity differs",
        )
        for entry in entries:
            block = plan_blocks[entry["block_id"]]
            _require(
                all(entry.get(key) == block.get(key) for key in ("experiment", "model", "configs")),
                "Bound rerun selection metadata differs",
            )
        source = sources.setdefault(
            plan_digest,
            {
                "original_root": plan_path.parent,
                "root": plan_path.parent,
                "blocks": plan_blocks,
                "selected": set(),
                "covered": set(),
                "covered_files": set(),
            },
        )
        _require(
            source["original_root"] == plan_path.parent,
            "Identical plan bytes have ambiguous source directories",
        )
        source["selected"].update(ids)
    return sources, paths, hashes


def _verify_data(bundle, *, manifest_paths=(), source_paths=(), replay_code_sha256=None):
    _require(bundle.get("schema") == SCHEMA, "Invalid R13 proof bundle schema")
    manifests, records = bundle.get("manifests"), bundle.get("proofs")
    _require(
        isinstance(manifests, list)
        and bool(manifests)
        and isinstance(records, list)
        and bool(records),
        "R13 proof bundle requires manifests and source proofs",
    )
    sources, bound_paths, bound_hashes = _manifest_sources(manifests)
    for path in manifest_paths:
        path = Path(path).resolve()
        _require(
            path in bound_paths and _digest(path) in bound_hashes,
            "Requested rerun manifest is not bound by the proof bundle",
        )
    overrides = {}
    for path in source_paths:
        path = Path(path).resolve()
        digest = _digest(path / "plan.json")
        _require(digest in sources and digest not in overrides, "Unknown or duplicate source")
        overrides[digest] = path
    for digest, path in overrides.items():
        sources[digest]["root"] = path

    seen_proofs, code_identity = set(), None
    for record in records:
        snapshot = Path(record["snapshot"]).resolve()
        proof_path = Path(record["proof"]).resolve()
        _require(proof_path not in seen_proofs, "Duplicate source replay proof")
        seen_proofs.add(proof_path)
        _require(
            _digest(snapshot / "snapshot.json") == record.get("snapshot_sha256")
            and _digest(proof_path) == record.get("proof_sha256"),
            "Bound replay proof or snapshot manifest hash changed",
        )
        copied, proof = _document(snapshot / "snapshot.json"), _document(proof_path)
        plan_digest = copied.get("source_plan_sha256")
        _require(plan_digest in sources, "Snapshot source is absent from the rerun manifests")
        source = sources[plan_digest]
        _require(
            copied.get("schema") == "freeze8-completed-source-snapshot-v1"
            and copied.get("source_selected_files_unchanged") is True
            and Path(copied["source"]).resolve() == source["original_root"],
            "Invalid or unstable completed-source snapshot",
        )
        hashes = copied.get("sha256")
        block_ids = copied.get("block_ids")
        _require(
            isinstance(hashes, dict)
            and isinstance(block_ids, list)
            and all(isinstance(identity, str) for identity in block_ids)
            and len(set(block_ids)) == len(block_ids)
            and copied.get("blocks") == len(block_ids)
            and copied.get("files") == len(hashes)
            and set(block_ids) <= source["blocks"].keys()
            and hashes.get("plan.json") == plan_digest,
            "Snapshot plan binding or coverage metadata differs",
        )
        expected_results = {}
        declared_blocks = set()
        for name, digest in hashes.items():
            parts = PurePosixPath(name).parts
            if name != "plan.json":
                _require(
                    len(parts) >= 2 and parts[0] in {"blocks", "episodes"},
                    "Snapshot contains files outside its completed blocks",
                )
                identity = parts[1].removesuffix(".json") if parts[0] == "blocks" else parts[1]
                _require(identity in block_ids, "Snapshot includes an uncompleted block")
                if parts[0] == "blocks":
                    _require(
                        len(parts) == 2 and parts[1] == identity + ".json",
                        "Invalid snapshot block filename",
                    )
                    declared_blocks.add(identity)
            _require(
                _digest(_safe_file(snapshot, name)) == digest
                and _digest(_safe_file(source["root"], name)) == digest,
                "Snapshot or current completed-source evidence hash changed",
            )
            if name.startswith("blocks/") or _result_identity(name) is not None:
                expected_results[name] = digest
            if name != "plan.json":
                source["covered_files"].add(name)
        _require(declared_blocks == set(block_ids), "Snapshot block coverage differs")
        result_ids = {
            identity
            for name in expected_results
            if (identity := _result_identity(name)) is not None
        }
        missing = _missing_attempts(snapshot, block_ids, source["blocks"], result_ids)
        units = proof.get("unit_results")
        _require(
            isinstance(units, list)
            and all(
                isinstance(unit, dict)
                and isinstance(unit.get("id"), str)
                and type(unit.get("consistent")) is bool
                for unit in units
            ),
            "Invalid source replay unit records",
        )
        unit_ids = [unit["id"] for unit in units]
        inconsistent = sorted(unit["id"] for unit in units if not unit["consistent"])
        code_hashes = proof.get("replay_code_sha256")
        _require(
            proof.get("schema") == "zh-source-replay-proof-v1"
            and proof.get("network_disabled") is True
            and proof.get("source_unchanged") is True
            and proof.get("code_unchanged") is True
            and proof.get("source_plan_sha256") == plan_digest
            and proof.get("source_result_sha256") == expected_results
            and len(set(unit_ids)) == len(unit_ids)
            and set(unit_ids) == result_ids
            and proof.get("units") == proof.get("replayed") == len(result_ids)
            and proof.get("consistent") == len(result_ids) - len(inconsistent)
            and proof.get("inconsistent") == len(inconsistent)
            and proof.get("inconsistent_ids") == inconsistent
            and proof.get("missing_source_attempts") == missing
            and proof.get("passed") is (not inconsistent and not missing)
            and isinstance(code_hashes, dict)
            and bool(code_hashes)
            and all(isinstance(value, str) and len(value) == 64 for value in code_hashes.values()),
            "Source replay proof identity, coverage or counters differ",
        )
        if code_identity is None:
            code_identity = code_hashes
        _require(code_hashes == code_identity, "Bundle proofs use different frozen code")
        if replay_code_sha256 is not None:
            _require(
                code_hashes == replay_code_sha256,
                "Source replay proof does not match the current frozen replay code",
            )
        _require(
            all(
                identity.split("/", 1)[0] in source["selected"]
                for identity in inconsistent + missing
            ),
            "Retained source block has a replay difference or missing attempt outside reruns",
        )
        source["covered"].update(block_ids)

    summaries = []
    for digest, source in sorted(sources.items()):
        root = source["root"]
        _require(_digest(root / "plan.json") == digest, "Current source plan hash changed")
        final_paths = sorted((root / "blocks").glob("*.json"))
        final_ids = {path.stem for path in final_paths}
        _require(
            final_ids <= source["blocks"].keys() and final_ids == source["covered"],
            "Current completed source blocks lack exact replay proof coverage",
        )
        current_files = {path.relative_to(root).as_posix() for path in final_paths}
        for identity in final_ids:
            episode_root = _safe_file(root, "episodes/" + identity)
            if episode_root.is_dir():
                current_files.update(
                    path.relative_to(root).as_posix()
                    for path in episode_root.rglob("*")
                    if path.is_file()
                )
        _require(
            current_files == source["covered_files"],
            "Current completed-source evidence has files outside the bound snapshots",
        )
        summaries.append({
            "source": str(root),
            "source_plan_sha256": digest,
            "completed_blocks": len(final_ids),
            "selected_rerun_blocks": len(source["selected"]),
            "selected_block_ids": sorted(source["selected"]),
            "retained_completed_blocks": len(final_ids - source["selected"]),
        })
    return {
        "schema": SCHEMA,
        "verified": True,
        "manifest_sha256": sorted(bound_hashes),
        "replay_code_sha256": code_identity,
        "sources": summaries,
    }


def create_bundle(manifests, snapshots, proofs, output, *, replay_code_sha256=None):
    """Create a fresh bundle from paired snapshots/proofs and every frozen selection.

    Additional snapshots of the same source can cover newly completed E4 blocks.
    They must use the same frozen replay code. Existing bundle files are immutable.
    """
    _require(len(snapshots) == len(proofs), "Each snapshot needs exactly one replay proof")
    output = Path(output).resolve()
    _require(not output.exists(), "R13 proof bundle output must be fresh")
    bundle = {
        "schema": SCHEMA,
        "manifests": [
            {"path": str(Path(path).resolve()), "sha256": _digest(path)} for path in manifests
        ],
        "proofs": [
            {
                "snapshot": str(Path(snapshot).resolve()),
                "snapshot_sha256": _digest(Path(snapshot) / "snapshot.json"),
                "proof": str(Path(proof).resolve()),
                "proof_sha256": _digest(proof),
            }
            for snapshot, proof in zip(snapshots, proofs, strict=True)
        ],
    }
    verified = _verify_data(bundle, replay_code_sha256=replay_code_sha256)
    _require(
        all(not output.is_relative_to(Path(source["source"])) for source in verified["sources"])
        and all(not output.is_relative_to(Path(snapshot).resolve()) for snapshot in snapshots),
        "R13 proof bundle must be outside read-only sources and snapshots",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(jcs_bytes(bundle) + b"\n")
    return verify_bundle(output, replay_code_sha256=replay_code_sha256)


def verify_bundle(
    path,
    *,
    expected_sha256=None,
    manifest_paths=(),
    source_paths=(),
    replay_code_sha256=None,
    supplemental_path=None,
):
    """Verify bound evidence and every current completed source block.

    Execution supplies replay_code_sha256=replay_code_hashes(). Reporting supplies
    the expected_sha256 stored in the frozen run plan and may omit current code
    hashes when reporting from a later merged checkout. Evidence is always checked.
    An optional supplemental bundle can append proofs for newly finalized E4 blocks;
    it must retain the original selection and every original proof record unchanged.
    """
    original_path = Path(path).resolve()
    original_digest = _digest(original_path)
    _require(
        expected_sha256 is None or original_digest == expected_sha256,
        "R13 proof bundle hash changed",
    )
    original = _document(original_path)
    actual_path, actual_digest, actual = original_path, original_digest, original
    if supplemental_path is not None:
        _require(expected_sha256 is not None, "Supplement requires the original frozen bundle hash")
        actual_path = Path(supplemental_path).resolve()
        _require(actual_path != original_path, "Supplement must be a new immutable bundle")
        actual_digest, actual = _digest(actual_path), _document(actual_path)
        original_proofs, actual_proofs = original.get("proofs"), actual.get("proofs")
        _require(
            original.get("schema") == actual.get("schema") == SCHEMA
            and original.get("manifests") == actual.get("manifests")
            and isinstance(original_proofs, list)
            and bool(original_proofs)
            and isinstance(actual_proofs, list)
            and all(record in actual_proofs for record in original_proofs),
            "Supplement changed the frozen selection or replaced an original proof record",
        )
    result = _verify_data(
        actual,
        manifest_paths=manifest_paths,
        source_paths=source_paths,
        replay_code_sha256=replay_code_sha256,
    )
    return {
        **result,
        "bundle_path": str(actual_path),
        "bundle_sha256": actual_digest,
        "original_bundle_path": str(original_path),
        "original_bundle_sha256": original_digest,
    }
