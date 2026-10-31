"""Exercise every frozen E4 pair without any model or external transport."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentdojo_calibrate import write  # noqa: E402
from agentdojo_runner import run_episode  # noqa: E402

from corridor.agent.llm import LLMClient  # noqa: E402
from corridor.bench.agentdojo_contract import e4_blocks, synthetic_context_guard  # noqa: E402
from corridor.bench.recording import network_disabled  # noqa: E402


def run(output):
    rows = []
    with network_disabled():
        for block in e4_blocks():
            if (block["model"], block["seed"], block["attack"], block["approval_mode"]) != (
                "qwen3-8b",
                1,
                "important_instructions",
                "prompt-bound",
            ):
                continue
            for config in block["configs"]:
                client = LLMClient(
                    mode="scripted",
                    config=config,
                    tool_namespace="agentdojo-banking-v1.2.2",
                    context_guard=synthetic_context_guard,
                )
                result = run_episode({**block, "config": config}, client)
                assert result["status"] == "valid_completed", result.get("reason")
                rows.append(result)
    summary = {
        "schema_version": "agentdojo-apparatus-smoke-v1",
        "passed": True,
        "mode": "scripted_noop_apparatus",
        "real_model_calls": 0,
        "rows": len(rows),
        "variants_per_config": {
            config: dict(Counter(row["variant"] for row in rows if row["config"] == config))
            for config in ("A0", "A5", "A_null", "spotlighting_with_delimiting")
        },
        "warning": "Pipeline coverage only; scripted no-op outcomes are not model results.",
    }
    write(output / "rows.json", rows)
    write(output / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/dryrun/agentdojo/apparatus")
    print(json.dumps(run(parser.parse_args().output)))
