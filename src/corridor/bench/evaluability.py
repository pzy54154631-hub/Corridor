"""Execution-scope availability, separate from missing runs and metric coverage."""

from collections import Counter


def promote_evaluability(row: dict, *sources: dict) -> dict:
    """Promote unavailable execution steps without inspecting diagnostic layers."""
    reasons = []

    def inspect(value):
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("evaluability") == "not_evaluable" or value.get("status") == "not_evaluable":
            reasons.extend((value.get("reason") or "execution_mapping_not_evaluable").split("; "))
        for key in ("steps", "tool_path", "gate_events"):
            steps = value.get(key, [])
            if isinstance(steps, dict):
                for step in steps.values():
                    inspect(step)
            else:
                inspect(steps)
        for key in ("result", "execution", "native_gate", "observed"):
            inspect(value.get(key))

    inspect(row)
    for source in sources:
        inspect(source)
    if reasons:
        row["evaluability"] = "not_evaluable"
        row["reason"] = "; ".join(dict.fromkeys(reasons))
        row["completed"] = False
    else:
        row.setdefault("evaluability", "not_run" if row.get("status") == "not_run" else "evaluated")
    return row


def unavailable(row: dict) -> bool:
    return promote_evaluability(dict(row))["evaluability"] == "not_evaluable"


def unavailable_reasons(rows: list[dict]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                reason
                for row in rows
                if unavailable(row)
                for reason in promote_evaluability(dict(row))["reason"].split("; ")
            ).items()
        )
    )
