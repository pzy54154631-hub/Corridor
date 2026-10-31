"""Explicit editorial projections of final_report_zh's domestic-report-v1 JSON.

No observations are aggregated here. Every measurement references a JSON path
and deployment selector; excluded pilots stay excluded. Missing collections
produce a disclosed not_run value, never a fabricated zero or borrowed result.
A deployment whose planned episodes were all left not_run (a partition still
unaccepted at the deadline) binds as not_run; a partly not_run deployment keeps
its BCR unresolved. E3 keys bind only the pre-specified stratum of the sealed
R16 paired sidecar. Paired-comparison
keys bind only the pre-registered attack-variant comparisons of that same
hash-checked sidecar, selected by stratum and left/right configuration (never by
index), cross-checked against the comparison's own case outcomes and against
the report's /paired row; glm-5.1 is exploratory and never paired.
benign.* keys project the 57-case benign corpus rows only, never the C2
legit_twin rows; e2.parameter_exact projects the same
apparatus intents object as the other e2 keys.
f10.* keys (f10_bindings) project only a hash-bound F10 report (FINAL-RESULTS-F10-zh.json,
outer f10_binding); an F10 report is never the F9 results and F9 keys never come from it.
"""

from __future__ import annotations

import codecs
import json
import re
from hashlib import sha256
from pathlib import Path

CONFIGS = ("A0", "A1", "A2", "A3", "A4", "A5", "B_prompt", "B_guard")
MODELS = ("gpt-6.1-sol", "qwen3.8-27b", "qwen3-8b", "qwen3.5-4b", "gpt-5.4-mini")
SIDECAR_SCHEMA = "r16-paired-sidecar-v1"
# Chosen before any F9 result was read: dialogue-paid, main deployment, full defense.
# The other E3 strata stay in the analysis report and are never a fallback.
E3_STRATUM = {
    "model": "gpt-6.1-sol",
    "provider": "openai",
    "corpus": "E3",
    "variant": "benign",
    "mode": "live-recorded",
    "exploratory": None,
    "config": "A5",
}
E3_IDENTITY = ("model", "provider", "config")
E3_KEYS = ("different_path_same_effect", "completion", "n", "consistent_cases")
# The pre-registered primary paired family: C2 attack variant, every measured
# non-A5 configuration of a formal deployment against A5 (PREREG R2-6; report
# /paired). The main deployment keeps the e1 prefix. glm-5.1 is exploratory: it is
# never paired, so no key exists for it and no other layer is a fallback.
PAIRED_RIGHT = "A5"
PAIRED_STRATUM = {
    "corpus": "C2",
    "variant": "attack",
    "mode": "live-recorded",
    "exploratory": None,
}
PAIRED = (
    ("cross.deepseek-flash.paired", "deepseek-flash", ("A0", "B_prompt")),
    ("cross.gpt-5.4-mini.paired", "gpt-5.4-mini", ("A0",)),
    ("e1.paired", MODELS[0], ("A0", "A1", "A2", "A3", "A4", "B_prompt", "B_guard")),
    ("cross.qwen3-8b.paired", "qwen3-8b", ("A0",)),
    ("cross.qwen3.5-4b.paired", "qwen3.5-4b", ("A0",)),
    ("cross.qwen3.8-27b.paired", "qwen3.8-27b", ("A0", "B_prompt")),
)
# The 57-case benign corpus: its own completion rate,
# never the C2 legit_twin BCR of e1/cross. A5 binds BCR (majority and all three) and the
# runtime errors; A0 binds only its not_evaluable count, since A0 is not comparable there.
BENIGN_SELECTOR = {"corpus": "benign", "variant": "legit_twin"}
BENIGN_MODE = "live-recorded"
BENIGN_PLANNED_UNITS = 171  # 57 cases x 3 seeds
BENIGN_NOT_EVALUABLE_REASON = "not_evaluable outside the frozen common immediate-transfer subset"
BENIGN = (
    ("gpt-6.1-sol", "A5", ("bcr", "bcr_all3", "runtime_errors")),
    ("qwen3.8-27b", "A5", ("bcr", "bcr_all3", "runtime_errors")),
    ("gpt-6.1-sol", "A0", ("not_evaluable",)),
    ("qwen3.8-27b", "A0", ("not_evaluable",)),
)
# Only these comparisons also expose the single "left only" cell as its own key.
PAIRED_LEFT_ONLY = {("e1.paired", "A0")}
# Display order of a four-cell vector: [both safe, left only, A5 only, both harm].
FOUR_CELLS = ("both_safe", "left_only", "right_only", "both_harm")
# F10 (PREREG-F10; F10-PLAN 9.7): a separate cross-deployment experiment,
# never pooled with F9. Core rows bind f10.<model>.<A0|A5>.<asr|bcr|m|n|unresolved|u95>;
# the single-listed rows bind f10.<C2-scope|C2-fault>.<model>.<A0|A5>.<asr|m|n|unresolved>;
# f10.cases.<C2|C2-scope|C2-fault|excluded> bind the planned case counts. M is fixed (the
# planned cases), n = M - u the decided cases.
# Models are the PREREG registry; a deployment dropped at P0 or downgraded binds not_run.
F10_BINDING_SCHEMA = "f10-binding-v1"
F10_MODELS = (
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-5-nano",
    "deepseek-flash",
    "glm-5.3",
    "glm-5.3-flash",
)
F10_CONFIGS = ("A0", "A5")
F10_CORE_METRICS = ("asr", "bcr", "m", "n", "unresolved", "u95")
F10_SINGLE_ROWS = ("C2-scope", "C2-fault")
F10_CORPORA = ("C2", *F10_SINGLE_ROWS)
F10_SINGLE_METRICS = ("asr", "m", "n", "unresolved")
F10_EXPERIMENT = "F10"


def _exact_json(raw: bytes, label: str):
    def reject_float(value):
        raise ValueError(f"Non-exact JSON number in {label}: {value}")

    return json.loads(raw, parse_float=reject_float)


# The sealed report is several gigabytes and is never parsed whole. It is streamed
# once, hashing every byte; only the top-level members the projections read are
# decoded, and every other member is walked one bounded value at a time and dropped.
REPORT_MEMBERS = frozenset((
    "schema_version",
    "dryrun",
    "summaries",
    "apparatus",
    "not_evaluable",
    "pilots",
    "r16_binding",
    "paired",
    "f10_binding",
))
F10_REPORT_MEMBERS = frozenset((
    "schema_version",
    "dryrun",
    "summaries",
    "not_evaluable",
    "pilots",
    "f10_binding",
))
# historical_reports nests whole reports, so skipped members are walked to row level.
SKIP_DEPTH = 3
_WHITESPACE = re.compile(r"[ \t\n\r]*")
# A number touching the buffer end may continue ("12" of "12.5", "1" of "1e3").
_NUMBER_TAIL = 4


def _reject_report_float(value):
    raise ValueError(f"Non-exact JSON number in report: {value}")


class _ReportStream:
    """Incremental reader of one JSON document; every byte it reads is hashed."""

    def __init__(self, handle, chunk: int):
        self._handle = handle
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("surrogatepass")
        self._json = json.JSONDecoder(parse_float=_reject_report_float)
        self._chunk = max(1, chunk)
        self.digest = sha256()
        self.buf = ""
        self.pos = 0
        self.eof = False

    def _more(self, minimum: int = 0) -> None:
        # Grow geometrically while one value spans blocks, keeping retries linear.
        block = self._handle.read(max(self._chunk, minimum))
        if block:
            self.digest.update(block)
            text = self._decoder.decode(block)
        else:
            text = self._decoder.decode(b"", final=True)
            self.eof = True
        self.buf = self.buf[self.pos :] + text
        self.pos = 0

    def peek(self) -> str:
        while True:
            self.pos = _WHITESPACE.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if self.eof:
                raise ValueError("Report JSON ends inside a value")
            self._more()

    def take(self, expected: str) -> str:
        found = self.peek()
        if found not in expected:
            raise ValueError(f"Malformed report JSON: expected one of {expected!r}, got {found!r}")
        self.pos += 1
        return found

    def value(self):
        self.peek()
        while True:
            try:
                result, end = self._json.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self._more(len(self.buf) - self.pos)
                continue
            if not self.eof and len(self.buf) - end <= _NUMBER_TAIL:
                self._more(len(self.buf) - self.pos)
                continue
            self.pos = end
            return result

    def skip(self, depth: int) -> None:
        opening = self.peek()
        if depth <= 0 or opening not in "[{":
            self.value()
            return
        self.pos += 1
        closing = "]" if opening == "[" else "}"
        if self.peek() == closing:
            self.pos += 1
            return
        while True:
            if opening == "{":
                if not isinstance(self.value(), str):
                    raise ValueError("Malformed report JSON: object key is not a string")
                self.take(":")
            self.skip(depth - 1)
            if self.take("," + closing) == closing:
                return

    def finish(self) -> None:
        while True:
            self.pos = _WHITESPACE.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                raise ValueError("Trailing data after the report JSON document")
            if self.eof:
                return
            self._more()


def _read_report(
    path: Path, *, chunk: int = 1 << 24, members: frozenset = REPORT_MEMBERS
) -> tuple[dict, str]:
    """Stream the report once: its projected members and the SHA-256 of all its bytes."""
    report: dict = {}
    seen: set[str] = set()
    with path.open("rb") as handle:
        stream = _ReportStream(handle, chunk)
        stream.take("{")
        if stream.peek() == "}":
            stream.pos += 1
        else:
            while True:
                key = stream.value()
                if not isinstance(key, str) or key in seen:
                    raise ValueError(f"Malformed or duplicate top-level report key: {key!r}")
                seen.add(key)
                stream.take(":")
                if key in members:
                    report[key] = stream.value()
                else:
                    stream.skip(SKIP_DEPTH)
                if stream.take(",}") == "}":
                    break
        stream.finish()
    return report, stream.digest.hexdigest()


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _binder(bindings: dict, name: str, digest: str, dryrun: bool):
    def bind(key, value, mode, denominator, pointer, *, status="observed", origin=None, **extra):
        if key in bindings:
            raise ValueError(f"Key bound twice: {key}")
        bindings[key] = dict(
            key=key,
            value=str(value),
            mode=str(mode),
            denominator=str(denominator),
            source=f"{origin or name}#{pointer}",
            report_sha256=digest,
            status=status,
            dryrun=dryrun,
            **extra,
        )

    return bind


def report_bindings(
    path: Path, sidecar: Path | None = None, sidecar_sha256: str | None = None
) -> dict:
    if (sidecar is None) != (sidecar_sha256 is None):
        raise ValueError("A sealed sidecar binds only with both its path and SHA-256")
    report, digest = _read_report(path)
    if report.get("schema_version") != "domestic-report-v1":
        raise ValueError("Unsupported final report JSON schema")
    if "f10_binding" in report:
        raise ValueError("An F10 report is never the F9 results; bind it only via f10_bindings")
    if not isinstance(report.get("summaries"), list) or not isinstance(
        report.get("apparatus"), dict
    ):
        raise ValueError("Malformed final report collections")
    bindings = {}
    bind = _binder(bindings, path.name, digest, report.get("dryrun") is True)

    def absent(key, pointer, selector=None):
        unavailable = [
            r
            for r in report.get("not_evaluable", [])
            if selector and all(r.get(k) == v for k, v in selector.items())
        ]
        status = "not_evaluable" if unavailable else "not_run"
        bind(
            key,
            status,
            "not_run",
            "not_available",
            pointer,
            status=status,
            selector=selector or {},
            excluded_pilot_rows=len(report.get("pilots", [])),
            reason="No matching report measurement; excluded pilots are never promoted",
        )

    def summary(prefix, model, config, metric):
        selector = dict(
            corpus="C2",
            model=model,
            config=config,
            variant="legit_twin" if metric == "bcr" else "attack",
        )
        candidates = [
            (i, row)
            for i, row in enumerate(report["summaries"])
            if all(row.get(k) == v for k, v in selector.items())
        ]
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous report deployment/mode for {prefix}.{metric}")
        key = f"{prefix}.{metric}"
        if not candidates:
            absent(key, "/summaries", selector)
            return
        index, row = candidates[0]
        pointer = f"/summaries/{index}"
        kwargs = dict(selector={**selector, "mode": row["mode"]})
        n = row["cases"]
        planned, not_run = row.get("planned"), row.get("not_run", 0)
        if planned is not None and not (_count(planned) and _count(not_run) and not_run <= planned):
            raise ValueError(f"Malformed planned/not_run counts for {key}")
        if planned and not_run == planned:
            # The frozen reporter keeps an unexecuted plan as a summaries row
            # (harm unknown, every attempt incomplete). That is not a measured
            # 0/N: the whole cell binds as not_run.
            bind(
                key,
                "not_run",
                "not_run",
                "not_available",
                pointer + "/not_run",
                status="not_run",
                planned_episodes=planned,
                not_run_episodes=not_run,
                reason="Every planned episode is not_run; an unaccepted plan is not an observed 0",
                **kwargs,
            )
            return
        if metric == "asr":
            measure = row["asr_any_at_3"]
            if measure["unresolved_cases"]:
                value = "unknown [" + ", ".join(measure["bounds"]) + "]"
                kwargs["status"] = "unresolved"
            else:
                value = f"{measure['events']}/{measure['denominator']}"
            pointer += "/asr_any_at_3"
            kwargs["interval"] = measure["interval"]
            kwargs["bounds"] = measure["bounds"]
        elif metric == "bcr":
            value = row["bcr"]["majority"]
            pointer += "/bcr/majority"
            if not_run:
                # Frozen BCR counts missing attempts as incomplete, so the
                # majority is only a lower bound while any episode is not_run.
                kwargs.update(
                    status="unresolved",
                    lower_bound=value,
                    planned_episodes=planned,
                    not_run_episodes=not_run,
                )
                value = f"unknown [≥{value}]"
        elif metric == "n":
            value = n
            pointer += "/cases"
        else:
            value = row["asr_any_at_3"]["unresolved_cases"]
            pointer += "/asr_any_at_3/unresolved_cases"
        if not n:
            value = "not_evaluable"
            kwargs["status"] = "not_evaluable"
        bind(key, value, row["mode"], f"{n} cases", pointer, **kwargs)

    for config in CONFIGS:
        for metric in ("asr", "bcr", "n"):
            summary(f"e1.{config}", MODELS[0], config, metric)
    for config in ("A0", "A5"):
        summary(f"e1.{config}", MODELS[0], config, "unresolved")
    for model in MODELS:
        for config in ("A0", "A5"):
            for metric in ("asr", "bcr"):
                summary(f"cross.{model}.{config}", model, config, metric)
    for model, config, metrics in BENIGN:
        benign_bindings(report, bind, absent, model, config, metrics)

    intents = report["apparatus"].get("intents")
    for key, field in (
        ("intent_accuracy", "intent_correct"),
        ("clarification", "clarification_correct"),
        ("n", "total"),
    ):
        name = f"e2.{key}"
        if not intents:
            absent(name, "/apparatus/intents")
            continue
        # e2 is a formal model measurement; a scripted pipeline run is not one.
        if intents.get("planner_mode") not in {"record", "live", "strict"}:
            absent(name, "/apparatus/intents/planner_mode")
            continue
        n = intents["total"]
        value = str(n) if key == "n" else f"{intents[field]}/{n}"
        bind(
            name,
            value if n else "not_evaluable",
            intents["planner_mode"],
            f"{n} utterances",
            f"/apparatus/intents/{field}",
            status="observed" if n else "not_evaluable",
            model=intents["model"],
        )
    parameter_exact_binding(intents, bind, absent)

    sidecar_bindings(report, digest, bind, sidecar, sidecar_sha256)
    return bindings


def parameter_exact_binding(intents, bind, absent) -> None:
    """e2.parameter_exact: utterances whose four parameter fields are all exact."""
    name = "e2.parameter_exact"
    pointer = "/apparatus/intents/parameter_exact_match"
    if not intents:
        absent(name, "/apparatus/intents")
        return
    if intents.get("planner_mode") not in {"record", "live", "strict"}:
        absent(name, "/apparatus/intents/planner_mode")
        return
    exact = intents.get("parameter_exact_match")
    if exact is None:
        absent(name, pointer)
        return
    correct = exact.get("correct") if isinstance(exact, dict) else None
    total = exact.get("total") if isinstance(exact, dict) else None
    accuracy = exact.get("accuracy") if isinstance(exact, dict) else None
    if not (
        _count(correct)
        and _count(total)
        and correct <= total
        and total == intents.get("total")
        and (accuracy is None or accuracy == f"{correct}/{total}")
    ):
        raise ValueError(f"Malformed {pointer}")
    bind(
        name,
        f"{correct}/{total}" if total else "not_evaluable",
        intents["planner_mode"],
        f"{total} utterances",
        pointer,
        status="observed" if total else "not_evaluable",
        model=intents.get("model"),
        parameter_extractor=intents.get("parameter_extractor"),
        definition="all four parameter fields exact (deterministic unsigned candidates)",
    )


def benign_bindings(report, bind, absent, model, config, metrics) -> None:
    """Project one 57-case benign corpus row; never the C2 legit_twin row, never aggregated."""
    prefix = f"benign.{model}.{config}"
    selector = dict(BENIGN_SELECTOR, model=model, config=config)
    candidates = [
        (index, row)
        for index, row in enumerate(report["summaries"])
        if all(row.get(k) == v for k, v in selector.items())
    ]
    if len(candidates) > 1:
        raise ValueError(f"Ambiguous report deployment/mode for {prefix}")
    if not candidates:
        for metric in metrics:
            absent(f"{prefix}.{metric}", "/summaries", selector)
        return
    index, row = candidates[0]
    pointer = f"/summaries/{index}"
    if row.get("mode") != BENIGN_MODE:
        raise ValueError(
            f"{prefix} has mode {row.get('mode')!r}, not the pre-specified {BENIGN_MODE!r}; "
            "no other mode is substituted"
        )
    n, planned, not_run = row.get("cases"), row.get("planned"), row.get("not_run", 0)
    if not (_count(n) and _count(planned) and _count(not_run) and not_run <= planned):
        raise ValueError(f"Malformed planned/not_run counts for {prefix}")
    if planned != BENIGN_PLANNED_UNITS:
        raise ValueError(
            f"{prefix} plans {planned} units, not the {BENIGN_PLANNED_UNITS} of the benign corpus"
        )
    kwargs = dict(selector={**selector, "mode": row["mode"]})
    if not_run == planned:
        for metric in metrics:
            bind(
                f"{prefix}.{metric}",
                "not_run",
                "not_run",
                "not_available",
                pointer + "/not_run",
                status="not_run",
                planned_episodes=planned,
                not_run_episodes=not_run,
                reason="Every planned episode is not_run; an unaccepted plan is not an observed 0",
                **kwargs,
            )
        return
    for metric in metrics:
        status = "observed"
        extra: dict = {}
        if metric in ("bcr", "bcr_all3"):
            field = "majority" if metric == "bcr" else "all_three"
            value = (row.get("bcr") or {}).get(field)
            if not (isinstance(value, str) and re.fullmatch(rf"\d+/{n}", value)):
                raise ValueError(f"Malformed {prefix} bcr/{field}")
            path, denominator = f"/bcr/{field}", f"{n} cases"
            if not n:
                value, status = "not_evaluable", "not_evaluable"
        elif metric in ("runtime_errors", "not_evaluable"):
            count = row.get(metric)
            if not (_count(count) and count <= planned):
                raise ValueError(f"Malformed {prefix} {metric}")
            if metric == "not_evaluable":
                reasons = row.get("not_evaluable_reasons") or {}
                if not (
                    isinstance(reasons, dict)
                    and set(reasons) <= {BENIGN_NOT_EVALUABLE_REASON}
                    and sum(reasons.values()) == count
                ):
                    raise ValueError(f"Unexpected not_evaluable reasons for {prefix}")
                extra["reasons"] = dict(reasons)
            value = f"{count}/{planned}"
            path, denominator = f"/{metric}", f"{planned} planned units"
        else:
            raise ValueError(f"No benign projection for {prefix}.{metric}")
        if not_run and status == "observed":
            # Missing attempts make any count from this row only a lower bound.
            extra.update(lower_bound=value, planned_episodes=planned, not_run_episodes=not_run)
            value, status = f"unknown [≥{value}]", "unresolved"
        bind(
            f"{prefix}.{metric}",
            value,
            row["mode"],
            denominator,
            pointer + path,
            status=status,
            **extra,
            **kwargs,
        )


def sealed_sidecar_bindings(
    report: dict, digest: str, name: str, sidecar: Path, sidecar_sha256: str
) -> dict:
    """Bind only the sidecar-sourced keys against a report's identity fields.

    For checking the sealed sidecar without parsing a multi-gigabyte report: the
    caller supplies the report's streamed SHA-256 and its apparatus, r16_binding,
    dryrun and paired members. All provenance checks are the full binder's.
    """
    bindings: dict = {}
    bind = _binder(bindings, name, digest, report.get("dryrun") is True)
    sidecar_bindings(report, digest, bind, sidecar, sidecar_sha256)
    return bindings


def sidecar_bindings(report, digest, bind, sidecar, sidecar_sha256) -> None:
    sealed = _sealed_sidecar(report, digest, sidecar, sidecar_sha256)
    e3_bindings(bind, sealed)
    paired_bindings(report, bind, sealed)


def _sealed_sidecar(report, digest, sidecar, sidecar_sha256) -> dict | None:
    """Load the sidecar only when its bytes hash to the sealed value and it names this report.

    apparatus.consistency is a scripted apparatus check, never the E3 result;
    with a sidecar present it is a conflicting second source and fails closed.
    """
    if sidecar is None:
        return None
    raw = sidecar.read_bytes()
    observed_digest = sha256(raw).hexdigest()
    if not (
        isinstance(sidecar_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", sidecar_sha256)
        and observed_digest == sidecar_sha256
    ):
        raise ValueError("Sidecar SHA-256 differs from the sealed value")
    if (report.get("apparatus") or {}).get("consistency"):
        raise ValueError(
            "Conflicting E3 sources: report apparatus.consistency and the sealed sidecar"
        )
    data = _exact_json(raw, "sidecar")
    if not isinstance(data, dict) or data.get("schema") != SIDECAR_SCHEMA:
        raise ValueError("Unsupported R16 sidecar schema")
    provenance = data.get("provenance")
    contract = (report.get("r16_binding") or {}).get("sealed_contract_sha256")
    if not (
        isinstance(provenance, dict)
        and isinstance(contract, str)
        and re.fullmatch(r"[0-9a-f]{64}", contract)
        and provenance.get("contract_sha256") == contract
        and provenance.get("report_sha256") == digest
    ):
        raise ValueError("Sidecar is not bound to this report and its sealed R16 contract")
    return dict(
        data=data,
        name=sidecar.name,
        extra=dict(sidecar_sha256=observed_digest, contract_sha256=contract),
    )


def e3_bindings(bind, sealed) -> None:
    """Bind e3.* only from the pre-specified stratum of a hash-bound sealed sidecar."""

    def not_run(origin, pointer, reason, **extra):
        for key in E3_KEYS:
            bind(
                f"e3.{key}",
                "not_run",
                "not_run",
                "not_available",
                pointer,
                status="not_run",
                origin=origin,
                selector=dict(E3_STRATUM),
                reason=reason,
                **extra,
            )

    if sealed is None:
        not_run(
            "sidecar:not_supplied",
            "/auxiliary/e3",
            "E3 binds only the sealed R16 sidecar stratum; no sidecar was supplied",
        )
        return
    data, extra, origin = sealed["data"], sealed["extra"], sealed["name"]
    strata = (data.get("auxiliary") or {}).get("e3")
    if not isinstance(strata, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("stratum"), dict) for entry in strata
    ):
        raise ValueError("Malformed sidecar auxiliary.e3")
    candidates = [
        (index, entry)
        for index, entry in enumerate(strata)
        if all(entry["stratum"].get(field) == E3_STRATUM[field] for field in E3_IDENTITY)
    ]
    if len(candidates) > 1:
        raise ValueError("Ambiguous E3 stratum for the pre-specified gpt-6.1-sol/A5 deployment")
    if not candidates:
        not_run(
            origin,
            "/auxiliary/e3",
            "Pre-specified E3 stratum absent; other strata are never a fallback",
            **extra,
        )
        return
    index, entry = candidates[0]
    # exploratory may be serialised as None or False; both mean the formal (non-exploratory) row.
    stratum = dict(entry["stratum"])
    if stratum.get("exploratory") is False:
        stratum["exploratory"] = None
    if stratum != E3_STRATUM:
        raise ValueError("E3 stratum identity differs from the pre-specified stratum")
    pointer = f"/auxiliary/e3/{index}"
    frozen = entry.get("frozen_consistency") or {}
    measure = frozen.get("different_path_same_effect") or {}
    operational = entry.get("operational_and_raw_attempt_sensitivity") or {}
    observations = operational.get("observations")
    planned = operational.get("planned_episodes")
    completed = operational.get("operational_completed")
    numerator, denominator = measure.get("numerator"), measure.get("denominator")
    if not (
        isinstance(observations, list)
        and all(isinstance(item, dict) for item in observations)
        and _count(planned)
        and len(observations) == planned
        and _count(completed)
        and completed <= planned
        and _count(numerator)
        and _count(denominator)
        and numerator <= denominator
        and _count(measure.get("unobserved"))
    ):
        raise ValueError("Malformed pre-specified E3 stratum")
    unaccepted = sum(
        (item.get("source_run") or {}).get("r16_partition_status") == "not_run"
        for item in observations
    )
    # Executed-but-not-evaluable rows also carry status not_run; they are measurements the
    # frozen metric excludes, not missing episodes. Only unexecuted planned rows are missing.
    missing = sum(
        item.get("status") == "not_run" and item.get("unavailable") is not True
        for item in observations
    )
    if not _count(frozen.get("not_run")) or frozen["not_run"] != missing:
        raise ValueError("E3 not_run count differs from the frozen consistency summary")
    if unaccepted or missing:
        not_run(
            origin,
            pointer,
            "Pre-specified E3 stratum is unaccepted or has not_run episodes; no fallback",
            unaccepted_episodes=unaccepted,
            not_run_episodes=missing,
            planned_episodes=planned,
            **extra,
        )
        return
    consistent, inconsistent = _e3_consistent_cases(frozen, measure)
    mode = E3_STRATUM["mode"]
    selection = dict(selector=dict(E3_STRATUM), **extra)
    status = "observed" if denominator else "not_evaluable"
    bind(
        "e3.different_path_same_effect",
        f"{numerator}/{denominator}" if denominator else "not_evaluable",
        mode,
        f"{denominator} comparisons",
        pointer + "/frozen_consistency/different_path_same_effect",
        status=status,
        origin=origin,
        unobserved=measure["unobserved"],
        **selection,
    )
    bind(
        "e3.n",
        str(denominator) if denominator else "not_evaluable",
        mode,
        f"{denominator} comparisons",
        pointer + "/frozen_consistency/different_path_same_effect/denominator",
        status=status,
        origin=origin,
        **selection,
    )
    bind(
        "e3.completion",
        f"{completed}/{planned}" if planned else "not_evaluable",
        mode,
        f"{planned} episodes",
        pointer + "/operational_and_raw_attempt_sensitivity/operational_completed",
        status="observed" if planned else "not_evaluable",
        origin=origin,
        **selection,
    )
    # 0/n different-path-same-effect is not a consistency failure.
    # Same denominator (observable cases), counted from the same frozen case list.
    bind(
        "e3.consistent_cases",
        f"{consistent}/{denominator}" if denominator else "not_evaluable",
        mode,
        f"{denominator} observable cases",
        pointer + "/frozen_consistency/cases",
        status=status,
        origin=origin,
        definition="observable case with path_count=1 and effect_count=1 across all runs",
        denominator_pointer=pointer + "/frozen_consistency/different_path_same_effect/denominator",
        inconsistent_cases=inconsistent,
        unobserved=measure["unobserved"],
        **selection,
    )


def _e3_consistent_cases(frozen: dict, measure: dict) -> tuple[int, list[str]]:
    """Count observable cases with one path and one effect; the list must match the summary."""
    cases = frozen.get("cases")
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ValueError("Malformed E3 frozen case list")
    observable = []
    for case in cases:
        flag = case.get("different_path_same_effect")
        fields = ("path_count", "effect_count", "observed_runs", "planned_runs")
        counts = [case.get(field) for field in fields]
        if not (
            isinstance(case.get("case_id"), str)
            and all(_count(value) for value in counts)
            and (flag is None or type(flag) is bool)
        ):
            raise ValueError("Malformed E3 frozen case list")
        if flag is None:
            continue
        paths, effects, observed, planned = counts
        # Frozen consistency.summarize_consistency: observable means every planned run
        # was measured and there are at least two of them.
        if not (
            observed == planned >= 2
            and paths >= 1
            and effects >= 1
            and flag == (paths > 1 and effects == 1)
        ):
            raise ValueError("E3 case entry contradicts the frozen consistency definition")
        observable.append(case)
    if (
        len(observable) != measure["denominator"]
        or len(cases) - len(observable) != measure["unobserved"]
        or sum(case["different_path_same_effect"] for case in observable) != measure["numerator"]
    ):
        raise ValueError("E3 case list differs from the frozen different_path_same_effect summary")
    consistent = [
        case for case in observable if case["path_count"] == 1 and case["effect_count"] == 1
    ]
    inconsistent = sorted(case["case_id"] for case in observable if case not in consistent)
    return len(consistent), inconsistent


def paired_bindings(report, bind, sealed) -> None:
    """Bind the pre-registered four-cell paired comparisons from the sealed sidecar only."""
    for prefix, model, lefts in PAIRED:
        for left in lefts:
            name = f"{prefix}.{left}_{PAIRED_RIGHT}"
            keys = [f"{name}.cells", f"{name}.unresolved"]
            if (prefix, left) in PAIRED_LEFT_ONLY:
                keys.append(f"{name}.left_only")
            _paired_comparison(report, bind, sealed, keys, model, left)


def _paired_comparison(report, bind, sealed, keys, model, left) -> None:
    label = f"{model} {left}-{PAIRED_RIGHT}"
    selector = dict(PAIRED_STRATUM, model=model, left=left, right=PAIRED_RIGHT)

    def not_run(origin, pointer, reason, **extra):
        for key in keys:
            bind(
                key,
                "not_run",
                "not_run",
                "not_available",
                pointer,
                status="not_run",
                origin=origin,
                selector=dict(selector),
                reason=reason,
                **extra,
            )

    if sealed is None:
        not_run(
            "sidecar:not_supplied",
            "/comparisons",
            "Paired comparisons bind only from the sealed R16 sidecar; no sidecar was supplied",
        )
        return
    data, extra, origin = sealed["data"], sealed["extra"], sealed["name"]
    comparisons = data.get("comparisons")
    if not isinstance(comparisons, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("stratum"), dict) for entry in comparisons
    ):
        raise ValueError("Malformed sidecar comparisons")
    # Select by layer identity and configurations, never by index. Mode and the
    # exploratory flag are checked afterwards so that a second mode or an exploratory
    # copy of the same comparison fails closed instead of being silently skipped.
    candidates = [
        (index, entry)
        for index, entry in enumerate(comparisons)
        if entry["stratum"].get("model") == model
        and entry["stratum"].get("corpus") == PAIRED_STRATUM["corpus"]
        and entry["stratum"].get("variant") == PAIRED_STRATUM["variant"]
        and entry.get("left") == left
        and entry.get("right") == PAIRED_RIGHT
    ]
    if len(candidates) > 1:
        raise ValueError(f"Ambiguous paired comparison (several layers or modes): {label}")
    if not candidates:
        listed = [
            index
            for index, entry in enumerate(data.get("not_run_comparisons") or [])
            if isinstance(entry, dict)
            and (entry.get("stratum") or {}).get("model") == model
            and entry.get("left") == left
            and entry.get("right") == PAIRED_RIGHT
        ]
        not_run(
            origin,
            "/comparisons",
            "Pre-specified paired comparison absent from the sealed sidecar; "
            "no other layer is a fallback",
            not_run_comparisons=listed,
            **extra,
        )
        return
    index, entry = candidates[0]
    stratum = dict(entry["stratum"])
    if stratum.get("exploratory") is False:
        stratum["exploratory"] = None
    if any(stratum.get(field) != value for field, value in PAIRED_STRATUM.items()):
        raise ValueError(f"Paired comparison layer differs from the pre-specified layer: {label}")
    coverage = [
        item
        for item in data.get("comparison_coverage") or []
        if isinstance(item, dict)
        and item.get("stratum") == entry["stratum"]
        and item.get("left") == left
        and item.get("right") == PAIRED_RIGHT
    ]
    if any(item.get("status") != "available" for item in coverage):
        raise ValueError(f"Sidecar coverage marks the paired comparison unavailable: {label}")
    frozen = entry.get("primary_frozen") or {}
    paired = frozen.get("paired") or {}
    sides = [frozen.get("left"), frozen.get("right")]
    observations = entry.get("pair_observations")
    episode_pairs = entry.get("episode_pairs")
    cells = paired.get("four_cells")
    unresolved = paired.get("unresolved_pairs")
    total = paired.get("paired_cases")
    if not (
        isinstance(observations, list)
        and all(isinstance(item, dict) for item in observations)
        and _count(episode_pairs)
        and len(observations) == episode_pairs
        and all(isinstance(side, dict) for side in sides)
        and all(isinstance(side.get("case_outcomes"), dict) for side in sides)
        and all(_count(side.get("not_run")) for side in sides)
        and isinstance(cells, dict)
        and set(cells) == set(FOUR_CELLS)
        and all(_count(cells[cell]) for cell in FOUR_CELLS)
        and _count(total)
        and sum(cells.values()) == total
        and isinstance(unresolved, list)
        and all(isinstance(case, str) for case in unresolved)
        and len(set(unresolved)) == len(unresolved)
    ):
        raise ValueError(f"Malformed paired comparison: {label}")
    _check_four_cells(sides, cells, unresolved, label)
    unaccepted = unexecuted = 0
    for item in observations:
        for side in ("left", "right"):
            observation = item.get(side)
            if not isinstance(observation, dict):
                unexecuted += 1
                continue
            source = observation.get("source_run") or {}
            unaccepted += source.get("r16_partition_status") == "not_run"
            # status not_run with evaluability not_evaluable is an executed, excluded
            # episode; any other not_run side was never executed.
            unexecuted += (
                observation.get("status") == "not_run"
                and observation.get("evaluability") != "not_evaluable"
            )
    pointer = f"/comparisons/{index}"
    if unaccepted or unexecuted or any(side["not_run"] for side in sides):
        not_run(
            origin,
            pointer,
            "Pre-specified paired comparison is unaccepted or has not_run episodes; no fallback",
            unaccepted_sides=unaccepted,
            unexecuted_sides=unexecuted,
            not_run_episodes=[side["not_run"] for side in sides],
            **extra,
        )
        return
    mode = stratum["mode"]
    cross = _report_pair(report, model, left, mode, cells, total, unresolved, label)
    pointer += "/primary_frozen/paired"
    # A four-cell vector is a count, not a rate: zero resolved pairs is not a k/0
    # ratio. With every pair unresolved the comparison is unresolved, never zero harm.
    status = "observed" if total else "unresolved" if unresolved else "not_evaluable"
    missing = status == "not_evaluable"
    common = dict(
        origin=origin,
        selector={**selector, "provider": stratum.get("provider")},
        paired_cases=total,
        unresolved_pairs=list(unresolved),
        episode_pairs=episode_pairs,
        four_cells={cell: cells[cell] for cell in FOUR_CELLS},
        report_cross_check=cross,
        **extra,
    )
    denominator = f"{total} paired cases"
    vector = "[" + ", ".join(str(cells[cell]) for cell in FOUR_CELLS) + "]"
    bind(
        keys[0],
        "not_evaluable" if missing else vector,
        mode,
        denominator,
        pointer + "/four_cells",
        status=status,
        order=list(FOUR_CELLS),
        **common,
    )
    bind(
        keys[1],
        "not_evaluable" if missing else str(len(unresolved)),
        mode,
        denominator,
        pointer + "/unresolved_pairs",
        status="not_evaluable" if missing else "observed",
        **common,
    )
    for key in keys[2:]:
        bind(
            key,
            "not_evaluable" if missing else str(cells["left_only"]),
            mode,
            denominator,
            pointer + "/four_cells/left_only",
            status=status,
            **common,
        )


def _check_four_cells(sides, cells, unresolved, label) -> None:
    """Recompute the frozen four cells from the comparison's own per-case outcomes."""
    left, right = (side["case_outcomes"] for side in sides)
    recomputed = dict.fromkeys(FOUR_CELLS, 0)
    missing = []
    for case in sorted(left.keys() | right.keys()):
        x = (left.get(case) or {}).get("harm")
        y = (right.get(case) or {}).get("harm")
        if x is None or y is None:
            missing.append(case)
            continue
        if type(x) is not bool or type(y) is not bool:
            raise ValueError(f"Malformed paired case outcome: {label}")
        recomputed[
            "both_harm" if x and y else "left_only" if x else "right_only" if y else "both_safe"
        ] += 1
    if recomputed != cells or missing != unresolved:
        raise ValueError(f"Paired four cells differ from the comparison's case outcomes: {label}")


def _report_pair(report, model, left, mode, cells, total, unresolved, label) -> str | None:
    """The report's own /paired row, when present, must carry the same four cells."""
    rows = [
        (index, row)
        for index, row in enumerate(report.get("paired") or [])
        if isinstance(row, dict)
        and row.get("model") == model
        and row.get("config") == left
        and row.get("mode") == mode
    ]
    if len(rows) > 1:
        raise ValueError(f"Ambiguous report /paired rows: {label}")
    if not rows:
        return None
    index, row = rows[0]
    if (row.get("four_cells"), row.get("paired_cases"), row.get("unresolved_pairs")) != (
        cells,
        total,
        unresolved,
    ):
        raise ValueError(f"Sidecar paired comparison differs from report /paired/{index}: {label}")
    return f"/paired/{index}"


def f10_bindings(path: Path, report_sha256: str) -> dict:
    """Project a hash-bound F10 report; never F9 keys, never pooled across deployments.

    Each cell key reads one frozen /summaries row selected by corpus, model, configuration
    and variant (legit_twin for BCR, attack otherwise). Denominators:
    M is the fixed number of planned cases of the corpus (from
    the outer f10_binding), N = M - u the decided cases, and a planned case without an
    evaluable row is unresolved, never dropped. asr binds e/M, or [e/M, (e+u)/M] when
    u>0; u95 is the frozen R2-7 interval upper bound, only when u=0. Whether a document
    may cite u95 follows the PREREG-F10 tier ("成立" only for e=0 and u=0), which this
    projection does not decide. f10.cases.* bind the planned case counts.
    """
    if not (isinstance(report_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", report_sha256)):
        raise ValueError("An F10 report binds only with its SHA-256")
    report, digest = _read_report(path, members=F10_REPORT_MEMBERS)
    if digest != report_sha256:
        raise ValueError("F10 report SHA-256 differs from the bound value")
    if report.get("schema_version") != "domestic-report-v1":
        raise ValueError("Unsupported F10 report JSON schema")
    binding = report.get("f10_binding")
    if not isinstance(binding, dict) or binding.get("schema") != F10_BINDING_SCHEMA:
        raise ValueError("Not an F10 report: the outer f10_binding is missing")
    if binding.get("dryrun") is not report.get("dryrun") or not isinstance(
        report.get("summaries"), list
    ):
        raise ValueError("Malformed F10 report binding or summaries")
    registry = binding.get("registry")
    if not isinstance(registry, list) or any(
        not isinstance(item, dict) or item.get("model") not in F10_MODELS for item in registry
    ):
        raise ValueError("F10 registry names a model outside the preregistered deployments")
    planned = binding.get("cases_by_corpus")
    excluded = binding.get("excluded_cases")
    if (
        not isinstance(planned, dict)
        or any(not _case_list(planned.get(corpus)) for corpus in F10_CORPORA)
        or not isinstance(excluded, list)
        or (excluded and not _case_list(excluded))
    ):
        raise ValueError("F10 binding lacks its planned and excluded cases")
    plan = binding.get("plan") if isinstance(binding.get("plan"), dict) else {}
    common = dict(
        experiment=F10_EXPERIMENT,
        f10_report_sha256=digest,
        f10_plan_sha256=plan.get("sha256"),
        f10_freeze_tag=binding.get("freeze_tag"),
    )
    bindings: dict = {}
    bind = _binder(bindings, path.name, digest, report.get("dryrun") is True)
    for corpus in F10_CORPORA:
        bind(
            f"f10.cases.{corpus}",
            len(planned[corpus]),
            "plan",
            "planned cases",
            f"/f10_binding/cases_by_corpus/{corpus}",
            cases=list(planned[corpus]),
            **common,
        )
    bind(
        "f10.cases.excluded",
        len(excluded),
        "plan",
        "cases never planned",
        "/f10_binding/excluded_cases",
        cases=list(excluded),
        **common,
    )
    for model in F10_MODELS:
        for config in F10_CONFIGS:
            for metric in F10_CORE_METRICS:
                key = f"f10.{model}.{config}.{metric}"
                cell = ("C2", model, config, planned["C2"])
                _f10_cell(report, bind, key, cell, metric, common)
    for corpus in F10_SINGLE_ROWS:
        for model in F10_MODELS:
            for config in F10_CONFIGS:
                for metric in F10_SINGLE_METRICS:
                    key = f"f10.{corpus}.{model}.{config}.{metric}"
                    cell = (corpus, model, config, planned[corpus])
                    _f10_cell(report, bind, key, cell, metric, common)
    return bindings


def _case_list(value) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(case, str) for case in value)
        and len(set(value)) == len(value)
    )


def _f10_cell(report, bind, key, cell, metric, common) -> None:
    corpus, model, config, planned_cases = cell
    selector = dict(
        corpus=corpus,
        model=model,
        config=config,
        variant="legit_twin" if metric == "bcr" else "attack",
    )
    candidates = [
        (index, row)
        for index, row in enumerate(report["summaries"])
        if all(row.get(k) == v for k, v in selector.items())
    ]
    if len(candidates) > 1:
        raise ValueError(f"Ambiguous F10 report deployment/mode for {key}")
    if not candidates:
        bind(
            key,
            "not_run",
            "not_run",
            "not_available",
            "/summaries",
            status="not_run",
            selector=selector,
            reason="No F10 report row; a dropped or downgraded deployment is never a zero",
            **common,
        )
        return
    index, row = candidates[0]
    pointer = f"/summaries/{index}"
    n, planned, not_run = row.get("cases"), row.get("planned"), row.get("not_run", 0)
    if not (_count(n) and _count(planned) and _count(not_run) and not_run <= planned):
        raise ValueError(f"Malformed planned/not_run counts for {key}")
    kwargs = dict(selector={**selector, "mode": row["mode"]}, **common)
    if planned and not_run == planned:
        bind(
            key,
            "not_run",
            "not_run",
            "not_available",
            pointer + "/not_run",
            status="not_run",
            planned_episodes=planned,
            not_run_episodes=not_run,
            reason="Every planned episode is not_run; an unexecuted cell is not an observed 0",
            **kwargs,
        )
        return
    if metric == "bcr":
        value = row["bcr"]["majority"]
        pointer += "/bcr/majority"
        if not n:
            value = "not_evaluable"
            kwargs["status"] = "not_evaluable"
        elif not_run:
            # Missing attempts count as incomplete, so the majority is only a lower bound.
            kwargs.update(
                status="unresolved",
                lower_bound=value,
                planned_episodes=planned,
                not_run_episodes=not_run,
            )
            value = f"unknown [≥{value}]"
        bind(key, value, row["mode"], f"{n} cases", pointer, **kwargs)
        return
    measure, outcomes = row["asr_any_at_3"], row.get("case_outcomes")
    if not isinstance(outcomes, dict) or not set(outcomes) <= set(planned_cases):
        raise ValueError(f"F10 report row has cases outside its planned corpus: {key}")
    fixed = len(planned_cases)
    absent = sorted(set(planned_cases) - set(outcomes))
    events = measure["events"]
    unresolved = measure["unresolved_cases"] + len(absent)
    if (
        events != sum(item.get("harm") is True for item in outcomes.values())
        or not n == measure["denominator"] == len(outcomes)
        or unresolved != fixed - sum(item.get("harm") is not None for item in outcomes.values())
    ):
        raise ValueError(f"Frozen counts do not reconcile with the planned cases: {key}")
    bounds = [f"{events}/{fixed}", f"{events + unresolved}/{fixed}"]
    kwargs.update(
        fixed_cases=fixed,
        decided_cases=fixed - unresolved,
        unresolved_cases=unresolved,
        absent_cases=absent,
        bounds=bounds,
        frozen_bounds=measure["bounds"],
        interval=None if unresolved else measure["interval"],
    )
    if metric == "asr":
        pointer += "/asr_any_at_3"
        if unresolved:
            value = "unknown [" + ", ".join(bounds) + "]"
            kwargs["status"] = "unresolved"
        else:
            value = f"{events}/{fixed}"
    elif metric == "u95":
        pointer += "/asr_any_at_3/interval/upper"
        if unresolved:
            # u>0: identification bounds only, never an interval (PREREG-F10 section 5).
            value = "unknown"
            kwargs["status"] = "unresolved"
        else:
            value = f"{100 * float(measure['interval']['upper']):.1f}%"
            kwargs["method"] = measure["interval"]["method"]
    elif metric == "m":
        value = fixed
        pointer = f"/f10_binding/cases_by_corpus/{corpus}"
    elif metric == "n":
        value = fixed - unresolved
        pointer += "/case_outcomes"
    else:
        value = unresolved
        pointer += "/asr_any_at_3/unresolved_cases"
    bind(key, value, row["mode"], f"{fixed} planned cases", pointer, **kwargs)
