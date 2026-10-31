"""eval-integrity-03: the preregistered H2 cost comparison is never produced.

H2 (PREREG section 3, R2-8, R2-14): A5 vs A0 BCR and median turns, compared only
on the common-capability subset.  ``final_report_zh.build_report`` computes that
comparison only for rows with ``corpus == "benign"`` and ``common_capability``.
``run_interaction_episode`` does emit ``corpus="benign"`` / ``variant="legit_twin"``,
but the live worker (``scripts/run_live_zh.py::worker``) overwrites every result
key with the plan row's values, so formal E2 rows carry ``corpus="E2"`` and
``variant="benign"``.  The "E2 共同能力的观测差值" section is therefore always
empty for the formal run, and the only E2 numbers left are the main-table BCRs,
which compare A5 on all tasks against A0 where every non-common task is a
``not_run`` counted as incomplete - exactly the comparison the report text says
it does not make.
"""

import importlib.util
from pathlib import Path

from corridor.schemas.base import loads_exact

ROOT = Path(__file__).resolve().parents[4]


def _report():
    path = ROOT / "scripts/final_report_zh.py"
    spec = importlib.util.spec_from_file_location("final_report_zh_rt5_03", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worker_rows(tmp_path, case_ids):
    from scripts import run_live_zh as runner

    rows = []
    for case_id in case_ids:
        for config in ("A0", "A5"):
            for seed in (1, 2, 3):
                payload = {
                    "priority": 4,
                    "experiment": "E2",
                    "model": "gpt-6.1-sol",
                    "provider": "openai",
                    "corpus": "E2",
                    "case_id": case_id,
                    "base_task_id": case_id,
                    "attack_template_id": "benign",
                    "family_id": "benign",
                    "seed": seed,
                    "variant": "benign",
                    "mode": "live-recorded",
                    "max_retries": 2,
                    "episode_timeout_seconds": 600,
                    "invalid_case": False,
                    "block_id": f"rt5-03-{case_id}-{seed}",
                    "config": config,
                    "status": "not_run",
                    "completed": False,
                    "prohibited_effect": None,
                }
                folder = tmp_path / case_id / config / str(seed)
                spec = {
                    "row": payload,
                    "client_profile": {"model": "gpt-6.1-sol"},
                    "episode_cap": "1/10",
                    "currency": "USD",
                    "cases": str(ROOT / "tests/fixtures/blind-zh-sample"),
                    "replay_dir": str(folder / "recording"),
                }
                # Same worker as the formal coordinator; scripted planner, no network.
                runner.worker(spec, folder / "result.json", "scripted", env_file=None)
                row = loads_exact((folder / "result.json").read_bytes())
                rows.append({**row, "mode": "live-recorded"})
    return rows


def test_worker_rows_reach_the_common_capability_comparison(tmp_path):
    rows = _worker_rows(tmp_path, ["ZH-TR-01"])
    assert all(row["common_capability"] is True for row in rows)
    assert {row["status"] for row in rows} == {"valid_completed"}
    _, evidence = _report().build_report(rows, dryrun=False)
    assert evidence["benign_common_comparisons"], (
        "formal E2 rows are relabelled corpus="
        f"{rows[0]['corpus']!r}/variant={rows[0]['variant']!r}; H2 comparison never computed"
    )
