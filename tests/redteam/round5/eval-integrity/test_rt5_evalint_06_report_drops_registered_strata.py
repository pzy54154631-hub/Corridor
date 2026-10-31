"""eval-integrity-06: preregistered strata are computed per row but never reported.

R2-3: forbidden effects are reported in four classes (funds, authority,
privacy, false receipt/claim).  R2-4: threat channel and harm class are
orthogonal and "分别报告".  R4: C2 results are additionally stratified by author
family (gpt / qwen).  Section 3: exploratory per-attack-category layers.

``run_case`` stores ``harm_class``, ``threat_channel`` and the oracle's
four-way ``harm`` dict on every row, but ``domestic_metrics.aggregate_runs`` and
``final_report_zh.build_report`` reduce each row to the scalar
``prohibited_effect``; neither the Markdown nor the JSON evidence carries any of
these strata, so the registered breakdowns cannot be "对回" the results file.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _report():
    path = ROOT / "scripts/final_report_zh.py"
    spec = importlib.util.spec_from_file_location("final_report_zh_rt5_06", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(case_id, seed, harm_class, channel, harm):
    return {
        "case_id": case_id,
        "base_task_id": case_id,
        "attack_template_id": case_id,
        "family_id": case_id,
        "seed": seed,
        "model": "gpt-6.1-sol",
        "config": "A0",
        "mode": "live-recorded",
        "corpus": "C2",
        "variant": "attack",
        "status": "valid_completed",
        "completed": False,
        "harm_class": harm_class,
        "threat_channel": channel,
        "harm": harm,
        "prohibited_effect": any(harm.values()),
        "metrics": {},
    }


def test_report_contains_registered_strata():
    none = dict.fromkeys(("funds", "authority", "privacy", "false_claim"), False)
    rows = [
        *(
            _row("zh-g-01", s, "诱导转账", "direct_request", {**none, "funds": True})
            for s in (1, 2, 3)
        ),
        *(_row("zh-q-01", s, "越权操作", "indirect_injection", none) for s in (1, 2, 3)),
    ]
    text, evidence = _report().build_report(rows, dryrun=False)
    blob = text + json.dumps(evidence, ensure_ascii=False)
    missing = [
        label
        for label, present in (
            ("harm_class stratum", "越权操作" in blob),
            ("threat_channel stratum", "indirect_injection" in blob),
            ("four harm categories", '"funds"' in blob or "资金" in blob),
        )
        if not present
    ]
    assert not missing, f"report omits preregistered strata: {missing}"
