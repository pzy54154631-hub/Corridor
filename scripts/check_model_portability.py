"""Run J01-J24 through local compatible HTTP, then repeat with networking disabled."""

import argparse
import os
from pathlib import Path
from unittest.mock import patch

from corridor.agent.llm import LLMClient
from corridor.bench.bank_cases import write_json
from corridor.bench.compatibility_mock import LocalCompatibilityMock
from corridor.bench.recording import network_disabled, observation_hash, replay_client
from corridor.cli.judge import IMPLEMENTED, run_bank_task

MODELS = {"deepseek-chat": "deepseek", "qwen-plus": "dashscope"}


def run(output: Path) -> bool:
    if (output / "summary.json").exists():
        raise ValueError("Use a new output directory; protocol evidence is not overwritten")
    results: dict[str, dict] = {}
    scripted: dict[str, str] = {}
    for task in IMPLEMENTED:
        result = run_bank_task(task, LLMClient(env_file=None), output / "scripted" / task)
        if not result["passed"]:
            raise ValueError(f"Scripted prerequisite failed: {task}")
        scripted[task] = observation_hash(result)
    for model, provider in MODELS.items():
        rows = []
        with LocalCompatibilityMock() as mock:
            client = mock.client(model, provider, output / model / "replay")
            for task in IMPLEMENTED:
                recorded = run_bank_task(task, client, output / model / "record" / task)
                rows.append({
                    "task_id": task,
                    "record_passed": recorded["passed"],
                    "record_hash": observation_hash(recorded),
                    "scripted_hash": scripted[task],
                    "source": "local-mock-scripted-equivalent",
                    "real_model_evaluation": "not_run",
                })
            strict = replay_client(client)
            profile = {
                "model": model,
                "provider": provider,
                "api_base": mock.api_base,
                "local_mock": True,
            }
        with network_disabled():
            for row in rows:
                repeated = run_bank_task(
                    row["task_id"], strict, output / model / "strict" / row["task_id"]
                )
                row.update(strict_passed=repeated["passed"], strict_hash=observation_hash(repeated))
                row["equivalent"] = row["scripted_hash"] == row["record_hash"] == row["strict_hash"]
                row["passed"] = row["record_passed"] and row["strict_passed"] and row["equivalent"]
        results[model] = {
            "profile": profile,
            "source": "local-mock-scripted-equivalent",
            "real_model_evaluation": "not_run",
            "synthetic_usage_and_cost": True,
            "passed": sum(row["passed"] for row in rows),
            "total": len(rows),
            "http_requests": client.api_calls,
            "strict_http_requests": strict.api_calls,
            "rows": rows,
        }
        write_json(output / model / "summary.json", results[model])
        print(
            f"{model}: {results[model]['passed']}/{len(rows)} local-mock equivalents; "
            "real model not_run",
            flush=True,
        )
    write_json(
        output / "summary.json", {"source": "local-mock-scripted-equivalent", "models": results}
    )
    report = [
        "# 国内模型协议可移植验证（本地 mock）",
        "",
        "本表只验证标准库 HTTP mock 的 function calling、完整 J 机制与断网严格复放。",
        "响应来自 scripted planner；未读取 .env、未调用真实模型。"
        "用量与费用字段均为合成值，不能解释为模型表现或账单。",
        "",
        "| 检查 | deepseek-chat | qwen-plus |",
        "|---|---|---|",
    ]
    for label, field in (
        ("J 本地 mock 录制与复放等价", "passed"),
        ("本地 HTTP 请求", "http_requests"),
        ("严格复放 HTTP 请求", "strict_http_requests"),
    ):
        report.append(
            "| "
            + label
            + " | "
            + " | ".join(
                str(results[model][field]) + ("/24" if field == "passed" else "")
                for model in MODELS
            )
            + " |"
        )
    report.extend([
        "| 真实模型验证 | not_run（等待凭据） | not_run（等待凭据） |",
        "",
        "| J 任务 | deepseek-chat | qwen-plus |",
        "|---|---|---|",
    ])
    for index, task in enumerate(IMPLEMENTED):
        report.append(
            "| "
            + task
            + " | "
            + " | ".join(
                "PASS" if results[model]["rows"][index]["passed"] else "FAIL" for model in MODELS
            )
            + " |"
        )
    (output / "RESULTS.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return all(result["passed"] == result["total"] for result in results.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("evidence/domestic/d2/model-portability")
    )
    options = parser.parse_args()
    # Explicit synthetic credentials and budgets; ignore any caller LLM configuration.
    clean = {key: value for key, value in os.environ.items() if not key.startswith("LLM_")}
    with patch.dict(os.environ, clean, clear=True):
        raise SystemExit(0 if run(options.output) else 1)
