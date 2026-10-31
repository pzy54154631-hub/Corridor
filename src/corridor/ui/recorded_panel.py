"""Read-only presentation of selected recorded and strictly replayed result objects."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from corridor.bench.live_zh import replay_projection
from corridor.crypto.jcs import H
from corridor.crypto.sign import RECEIPT, verify
from corridor.gateway.audit import AuditLog
from corridor.gateway.privacy import PIIVault
from corridor.schemas.bank import BankReceipt
from corridor.schemas.base import loads_exact
from corridor.schemas.state import BankState


def _harm(row: dict[str, Any]) -> bool | None:
    attempts = row.get("attempts") or [row]
    values = [value for attempt in attempts for value in attempt.get("harm", {}).values()]
    if any(value is True for value in values):
        return True
    categories = {"funds", "privacy", "authority", "false_claim"}
    if not values or any(not categories <= attempt.get("harm", {}).keys() for attempt in attempts):
        return None
    return False if all(value is False for value in values) else None


def select_recorded_pair(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Apply the predeclared formal E1/C2 rule; use lowest numeric seed for a tie.

    The caller supplies rows from the designated formal run. Notification labels
    never affect selection. Unknown harm is not evidence of absence of harm.
    """
    pairs: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in rows:
        if (
            row.get("experiment") != "E1"
            or row.get("corpus") != "C2"
            or row.get("model") != "gpt-6.1-sol"
            or row.get("config") not in {"A0", "A5"}
            or row.get("variant") != "attack"
            or row.get("replay_passed") is not True
            or row.get("rehearsal") is True
            or row.get("in_denominator") is False
        ):
            continue
        if not isinstance(row.get("case_id"), str) or type(row.get("seed")) is not int:
            raise ValueError("Invalid recording identity")
        identity = row["case_id"], row["seed"]
        pair = pairs.setdefault(identity, {})
        if row["config"] in pair:
            raise ValueError("Duplicate recording identity")
        pair[row["config"]] = row
    for (case_id, seed), pair in sorted(pairs.items()):
        if set(pair) == {"A0", "A5"} and _harm(pair["A0"]) is True and _harm(pair["A5"]) is False:
            return {"case_id": case_id, "seed": seed, "A0": pair["A0"], "A5": pair["A5"]}
    return None


def recording_rows(root: Path) -> list[dict[str, Any]]:
    """Read final units and verify their attempts, independently of other configs."""
    rows = []
    for path in sorted((root / "blocks").glob("*.json")):
        block = loads_exact(path.read_bytes())
        for row in block.get("rows", []):
            matches = []
            for attempt in row.get("attempts", []):
                try:
                    folder = _inside(
                        root, "episodes", row["block_id"], row["config"], str(attempt["attempt"])
                    )
                    original = loads_exact((folder / "result.json").read_bytes())
                    strict = loads_exact((folder / "strict/result.json").read_bytes())
                    matches.append(
                        strict.get("mode") == "strict-replay"
                        and H(replay_projection(original)) == H(replay_projection(strict))
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    matches.append(False)
            rows.append({
                **row,
                "replay_passed": bool(matches) and all(matches),
                "block_replay_passed": block.get("replay_passed"),
            })
    return rows


def _inside(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Recording path escapes selected directory")
    return path


def _attempt_view(root: Path, row: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    folder = _inside(root, "episodes", row["block_id"], row["config"], str(attempt["attempt"]))
    original = loads_exact((folder / "result.json").read_bytes())
    strict = loads_exact((folder / "strict" / "result.json").read_bytes())
    strict_matches = strict.get("mode") == "strict-replay" and H(replay_projection(original)) == H(
        replay_projection(strict)
    )
    if not strict_matches:
        raise ValueError("Recorded and strict result projections differ")
    identity = ("case_id", "seed", "config", "model", "variant")
    if any(original.get(key) != row.get(key) for key in identity):
        raise ValueError("Recorded result identity differs from selected unit")
    episode = folder / "strict" / "episode"
    initial = BankState.model_validate(loads_exact((episode / "initial_state.json").read_bytes()))
    audit = AuditLog(episode / "audit.jsonl", episode / "audit.checkpoint.json")
    if not audit.verify():
        raise ValueError("Recorded audit chain failed verification")
    bank_events = strict.get("episode", {}).get("bank_effects", [])
    receipts = [
        BankReceipt.model_validate(event)
        for event in bank_events
        if event.get("receipt_type") == "bank"
    ]
    if not all(
        verify(initial.roots.bank_pubkey, RECEIPT, receipt, receipt.sig) for receipt in receipts
    ):
        raise ValueError("Recorded bank receipt signature failed verification")
    decisions = loads_exact((episode / "decisions.json").read_bytes())
    observations = strict.get("episode", {}).get("observations", [])
    submitted = [
        observation["payload"]
        for observation in observations
        if observation.get("boundary") == "tool_parameters"
        and isinstance(observation.get("payload"), dict)
        and "action_id" in observation["payload"]
        and "kind" in observation["payload"]
    ]
    result = {
        "attempt": attempt["attempt"],
        "status": strict.get("status"),
        "completed": strict.get("completed"),
        "model_choices": [
            {key: step[key] for key in ("turn", "kind", "target_role") if key in step}
            for step in strict.get("steps", [])
        ],
        "submitted_proposals": submitted,
        "dispatched_actions": [
            action for step in strict.get("steps", []) for action in step.get("actual_actions", [])
        ],
        "preparation_diagnostics": decisions,
        "kernel_enforced": row["config"] == "A5",
        "gateway_rules": [step.get("rule_ids", []) for step in strict.get("steps", [])],
        "bank_dispatches": strict.get("bank_dispatches"),
        "receipt_backed_dispatches": len(receipts),
        "dispatch_intents": sum(record["event"] == "dispatch_intent" for record in audit.records()),
        "dispatch_count_scope": (
            "exact call count absent unless recorded; receipts and intents are separate"
        ),
        "bank_unknown": strict.get("episode", {}).get("bank_unknown", False),
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "receipt_signatures_valid": True,
        "audit_valid": True,
        "audit_checkpoint": audit.checkpoint(),
        "strict_projection_matches": strict_matches,
        "strict_projection_hash": H(replay_projection(strict)),
        "harm": attempt.get("harm"),
    }
    PIIVault().ensure_safe(result, rule="PII-OUT")
    return result


def load_recorded_pair(
    root: Path, *, case_id: str, seed: int, model: str = "gpt-6.1-sol", variant: str = "attack"
) -> dict[str, Any]:
    """Verify only the requested A0/A5 pair; never execute a model or mutate source."""
    selected = [
        row
        for row in recording_rows(root)
        if row.get("case_id") == case_id
        and row.get("seed") == seed
        and row.get("model") == model
        and row.get("variant") == variant
        and row.get("config") in {"A0", "A5"}
    ]
    if len(selected) != 2 or {row["config"] for row in selected} != {"A0", "A5"}:
        raise ValueError("Exactly one matched A0/A5 pair is required")
    if any(row["replay_passed"] is not True for row in selected):
        raise ValueError("A selected unit did not pass strict replay")
    return {
        "case_id": case_id,
        "seed": seed,
        "model": model,
        "rehearsal": any(row.get("rehearsal") is True for row in selected),
        "verification": "stored_strict_results_compared_offline",
        "units": {
            row["config"]: [_attempt_view(root, row, attempt) for attempt in row["attempts"]]
            for row in selected
        },
    }


def render_recorded_panel():
    import streamlit as st

    from corridor.ui.messages import tr

    st.subheader(tr("Recorded model evidence"))
    st.caption(
        tr(
            "Read-only offline verification of recorded strict results; "
            "no model request is made here."
        )
    )
    root_text = st.text_input(tr("Recording directory (local copy)"), key="recorded_root")
    case_id = st.text_input(tr("Selected public case ID"), key="recorded_case")
    seed = st.number_input(tr("Selected seed"), min_value=0, value=1, step=1, key="recorded_seed")
    public_description = st.text_input(
        tr("Public description of this case"), key="recorded_description"
    )
    cases_text = st.text_input(
        tr("Local case fixture directory for strict replay"), key="recorded_cases"
    )
    if st.button(tr("Load and verify selected A0 / A5"), key="recorded_load"):
        try:
            st.session_state["recorded_pair"] = load_recorded_pair(
                Path(root_text), case_id=case_id, seed=seed
            )
        except (OSError, ValueError, KeyError, TypeError):
            st.session_state.pop("recorded_pair", None)
            st.error(tr("The selected recording or its strict evidence could not be verified."))
    if st.button(
        tr("Run offline strict replay of selected A0 / A5"),
        key="recorded_replay",
        disabled=not root_text or not case_id or not cases_text,
    ):
        from corridor.ui.recorded_replay import replay_recorded_pair

        try:
            with st.spinner(tr("Running and verifying results…")):
                st.session_state["recorded_pair"] = replay_recorded_pair(
                    Path(root_text), case_id=case_id, seed=seed, cases=Path(cases_text)
                )
        except (OSError, ValueError, KeyError, TypeError):
            st.session_state.pop("recorded_pair", None)
            st.error(tr("The selected recording or its strict evidence could not be verified."))
    pair = st.session_state.get("recorded_pair")
    if pair:
        st.success(
            tr("New network-disabled strict replay verified")
            if pair["verification"] == "new_network_disabled_strict_replay"
            else tr("Stored strict-result evidence verified offline")
        )
        st.caption(
            tr("Development rehearsal · excluded from formal results")
            if pair["rehearsal"]
            else tr("Selected recorded pair")
        )
        if public_description:
            st.write(public_description)
        st.write(f"{pair['case_id']} · seed {pair['seed']} · {pair['model']}")
        for column, config in zip(st.columns(2), ("A0", "A5"), strict=True):
            with column:
                st.markdown("### " + config)
                for attempt in pair["units"][config]:
                    st.caption(tr("Attempt: ") + str(attempt["attempt"]))
                    st.metric(
                        tr("Receipt-backed bank dispatches"), attempt["receipt_backed_dispatches"]
                    )
                    if attempt["bank_dispatches"] is None:
                        st.caption(
                            tr(
                                "Exact bank call count was not recorded; "
                                "receipts exclude unresolved dispatches."
                            )
                        )
                    if not attempt["kernel_enforced"]:
                        st.caption(
                            tr(
                                "Baseline has no kernel enforcement; "
                                "recorded setup decisions are shown as evidence only."
                            )
                        )
                    st.success(
                        tr("Strict result match · receipt signatures and audit chain verified")
                    )
                    st.json(attempt)
    else:
        # Offline verification of a chosen recording; results live in the submission.
        st.info(tr("polish.recorded.pending"))
