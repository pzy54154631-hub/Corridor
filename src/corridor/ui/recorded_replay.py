"""UI adapter for the existing network-disabled strict replay CLI worker."""

import subprocess
import sys
from pathlib import Path
from typing import Any

from corridor.bench.bank_cases import write_json
from corridor.crypto.jcs import H
from corridor.schemas.base import loads_exact
from corridor.ui.recorded_panel import _inside, load_recorded_pair, recording_rows


def replay_recorded_pair(root: Path, *, case_id: str, seed: int, cases: Path) -> dict[str, Any]:
    """Rebase paths into local copies; strict workers cannot call model transport.

    No original spec path is followed. The existing CLI owns replay semantics
    and its network-disabled context. Output remains under this checkout's .tmp.
    """
    if not cases.is_dir():
        raise ValueError("A local case fixture directory is required")
    # Verify the chosen stored pair before asking the existing worker to replay it.
    load_recorded_pair(root, case_id=case_id, seed=seed)
    rows = [
        row
        for row in recording_rows(root)
        if row.get("case_id") == case_id
        and row.get("seed") == seed
        and row.get("model") == "gpt-6.1-sol"
        and row.get("variant") == "attack"
        and row.get("config") in {"A0", "A5"}
    ]
    checkout = Path(__file__).resolve().parents[3]
    output = (
        checkout
        / ".tmp"
        / "ui-recorded-replay"
        / H({"source": str(root.resolve()), "case_id": case_id, "seed": seed})[:16]
    )
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Replay output must be separate from recording source")
    for row in rows:
        for attempt in row["attempts"]:
            parts = ("episodes", row["block_id"], row["config"], str(attempt["attempt"]))
            source = _inside(root, *parts)
            destination = _inside(output, *parts)
            spec = loads_exact((source / "spec.json").read_bytes())
            if any(
                spec["row"].get(key) != row.get(key)
                for key in ("case_id", "seed", "model", "config", "variant", "block_id")
            ):
                raise ValueError("Replay spec differs from selected recording")
            spec = {
                **spec,
                "cases": str(cases.resolve()),
                "replay_dir": str((source / "recording").resolve()),
                "result_path": str((source / "result.json").resolve()),
            }
            write_json(destination / "spec.json", spec)
            write_json(
                destination / "result.json", loads_exact((source / "result.json").read_bytes())
            )
            replay_path = destination / "strict/result.json"
            # Remove only this prior generated result, so failed reruns cannot reuse it.
            if replay_path.is_file():
                replay_path.unlink()
            try:
                execution = subprocess.run(
                    [
                        sys.executable,
                        str(checkout / "scripts/run_live_zh.py"),
                        "--worker",
                        str(destination / "spec.json"),
                        "--strict",
                        "--output",
                        str(replay_path),
                    ],
                    cwd=checkout,
                    capture_output=True,
                    timeout=630,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise ValueError("Strict replay worker exceeded its limit") from error
            if execution.returncode or not replay_path.is_file():
                raise ValueError("Strict replay worker did not complete")
    by_block: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_block.setdefault(row["block_id"], []).append(row)
    for block_id, block_rows in by_block.items():
        write_json(
            output / "blocks" / f"{block_id}.json",
            {
                "rows": block_rows,
                "replay_passed": True,
            },
        )
    result = load_recorded_pair(output, case_id=case_id, seed=seed)
    result["verification"] = "new_network_disabled_strict_replay"
    result["replay_output"] = str(output)
    return result
