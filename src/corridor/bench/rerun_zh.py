"""R13 plan-only selection, content-bound sealing, and whole-block reruns."""

from hashlib import sha256
from pathlib import Path

from corridor.bench.bank_cases import write_json
from corridor.bench.live_zh import EPISODE_TIMEOUT_SECONDS, MAX_RETRIES
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

SCHEMA = "zh-rerun-manifest-v1"
FIRST_RUN_SCHEMA = "zh-rerun-manifest-v2"
METADATA = ("block_id", "experiment", "model", "configs")


def require_unstarted(source, block_id):
    """Absence of a final result alone does not prove a block was never attempted."""
    source = Path(source)
    if any(
        (source / relative).exists()
        for relative in (
            f"blocks/{block_id}.json",
            f"episodes/{block_id}",
            f"progress/{block_id}.jsonl",
        )
    ):
        raise ValueError("First-run source has execution evidence: " + block_id)


def create_manifest(plan_path, source_run, selection, block_ids, *, allow_first_run=False):
    """Read plan metadata and hash result bytes without interpreting any outcome."""
    plan_path = Path(plan_path).resolve()
    raw = plan_path.read_bytes()
    plan = loads_exact(raw)
    ids = set(block_ids)
    blocks = [block for block in plan["blocks"] if block["block_id"] in ids]
    if not ids or len(ids) != len(block_ids) or len(blocks) != len(ids):
        raise ValueError("Rerun selection requires unique known source block IDs")
    entries = []
    for block in blocks:
        result = plan_path.parent / "blocks" / (block["block_id"] + ".json")
        entry = {
            **{key: block[key] for key in METADATA},
            "source_result_sha256": sha256(result.read_bytes()).hexdigest()
            if result.is_file()
            else None,
        }
        if allow_first_run:
            entry["execution_kind"] = "rerun" if result.is_file() else "first_run"
            if entry["execution_kind"] == "first_run":
                require_unstarted(plan_path.parent, block["block_id"])
        entries.append(entry)
    return {
        "schema": FIRST_RUN_SCHEMA if allow_first_run else SCHEMA,
        "selection": selection,
        "source_run": source_run,
        "source_plan": str(plan_path),
        "source_plan_sha256": sha256(raw).hexdigest(),
        "merge_rule": "whole_block_replacement",
        "blocks": entries,
    }


def load_manifest(path, *, require_final=False):
    """Select exact source objects; never rebuild identities or reorder configurations."""
    path = Path(path)
    raw = path.read_bytes()
    manifest = loads_exact(raw)
    if raw != jcs_bytes(manifest):
        raise ValueError("Rerun manifests must use canonical JSON bytes")
    if (
        manifest.get("schema") not in {SCHEMA, FIRST_RUN_SCHEMA}
        or manifest.get("merge_rule") != "whole_block_replacement"
    ):
        raise ValueError("Invalid rerun manifest schema or merge rule")
    source_plan = Path(manifest["source_plan"])
    plan_raw = source_plan.read_bytes()
    if sha256(plan_raw).hexdigest() != manifest["source_plan_sha256"]:
        raise ValueError("Rerun source plan hash changed")
    plan = loads_exact(plan_raw)
    entries = manifest["blocks"]
    ids = [entry["block_id"] for entry in entries]
    selected = [block for block in plan["blocks"] if block["block_id"] in set(ids)]
    if not ids or len(set(ids)) != len(ids) or [b["block_id"] for b in selected] != ids:
        raise ValueError("Rerun source block identity/order mismatch")
    pending = []
    first_run = []
    for entry, block in zip(entries, selected, strict=True):
        if any(entry[key] != block[key] for key in METADATA):
            raise ValueError("Rerun source block metadata changed")
        if (
            block.get("max_retries", MAX_RETRIES) != MAX_RETRIES
            or block.get("episode_timeout_seconds", EPISODE_TIMEOUT_SECONDS)
            != EPISODE_TIMEOUT_SECONDS
        ):
            raise ValueError("Rerun source retry/timeout limits cannot be changed")
        digest = entry.get("source_result_sha256")
        if manifest["schema"] == FIRST_RUN_SCHEMA:
            kind = entry.get("execution_kind")
            if kind not in {"rerun", "first_run"} or (kind == "first_run") != (digest is None):
                raise ValueError("Invalid first-run source binding")
            if kind == "first_run":
                require_unstarted(source_plan.parent, block["block_id"])
                first_run.append(block["block_id"])
                continue
        if digest is None:
            pending.append(block["block_id"])
            continue
        result = source_plan.parent / "blocks" / (block["block_id"] + ".json")
        if not result.is_file() or sha256(result.read_bytes()).hexdigest() != digest:
            raise ValueError("Rerun source result hash changed or result missing")
        if require_final:
            report = loads_exact(result.read_bytes())
            rows = report.get("rows", [])
            if (
                report.get("block_id", block["block_id"]) != block["block_id"]
                or [row.get("config") for row in rows] != block["configs"]
                or any(
                    any(row.get(key) != value for key, value in block.items() if key != "configs")
                    or "status" not in row
                    for row in rows
                )
            ):
                raise ValueError("Rerun requires a final result for the complete source block")
    if require_final and pending:
        raise ValueError(
            "Rerun source hashes pending; seal completed blocks first: " + ", ".join(pending)
        )
    return selected, {
        "row_set": "rerun",
        "source_plan_sha256": manifest["source_plan_sha256"],
        "source_directory": str(source_plan.parent.resolve()),
        "source_plan": str(source_plan.resolve()),
        "source_run": manifest["source_run"],
        "rerun_manifest_sha256": sha256(raw).hexdigest(),
        "rerun_manifest": manifest,
        "replaced_block_ids": ids,
        "first_run_block_ids": first_run,
        "merge_rule": "whole_block_replacement",
    }


def seal_manifest(path, output):
    """Seal previously pending hashes in a new artifact; preserve selection evidence."""
    path, output = Path(path), Path(output)
    if output.exists() or output.resolve() == path.resolve():
        raise ValueError("Sealing requires a new output file")
    _, identity = load_manifest(path)
    manifest = identity["rerun_manifest"]
    sealed = create_manifest(
        manifest["source_plan"],
        manifest["source_run"],
        manifest["selection"],
        [row["block_id"] for row in manifest["blocks"]],
        allow_first_run=manifest["schema"] == FIRST_RUN_SCHEMA,
    )
    if any(
        row["source_result_sha256"] is None and row.get("execution_kind") != "first_run"
        for row in sealed["blocks"]
    ):
        raise ValueError("Cannot seal pending source results")
    sealed["selection_manifest_sha256"] = sha256(path.read_bytes()).hexdigest()
    write_json(output, sealed)
    load_manifest(output, require_final=True)
    return sealed
