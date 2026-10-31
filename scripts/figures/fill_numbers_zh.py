"""Bind Chinese submission placeholders to explicit, sourced final-report rows.

Accept the native domestic-report-v1 JSON produced by final_report_zh.py, or
an explicit legacy key/value/mode/denominator/source table. Keys project exact
report fields and never aggregate across deployments.
Default filling fails closed. --audit only inventories pending values; it does
not certify a final submission. PDFs and raster figures must be regenerated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TOKEN = re.compile(r"\{\{NUM:([A-Za-z0-9_.@-]+)\}\}")
ANY_TOKEN = re.compile(r"\{\{[^{}]+\}\}")
TEXT = {".md", ".txt", ".json", ".svg", ".srt", ".html", ".csv"}
# Editorial to-do markers (e.g. "<!-- TODO-BATCH3: ... -->") record source text that
# still waits on the final code baseline or a pending run; like a residual {{...}}
# placeholder they keep a rebuild from ever being final-ready.
PENDING_MARKER = re.compile(r"TODO-[A-Z][A-Z0-9]*")
OFFICE = {".docx", ".pptx"}
EDITORIAL_KEYS = {"TEAM_NAME", "R16_STATUS", "REPO_URL", "FLAGSHIP_VERDICT"}
# Pending editorial slots stay visible in the source until a human approves them.
PENDING_EDITORIAL = {"{{R16_STATUS}}", "{{REPO_URL}}", "{{FLAGSHIP_VERDICT}}"}
# Rehearsal markers and unresolved slots never stand in for an approved editorial value.
UNAPPROVED_EDITORIAL = re.compile(r"(?i:dryrun)|PENDING|\{\{")
# The flagship verdict is spoken only after strict replay of both recorded sides;
# it may take one of these pre-approved sentences and nothing else.
FLAGSHIP_VERDICTS = (
    "两侧提案不同，严格回放后均被同一规则编号拒绝。",
    "两侧结果不同，差异照实保留，见技术文档第07章。",
    "跨模型压轴录制未完成，本段不作裁决。",
)
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# E3 and paired-comparison keys come only from the hash-checked sealed R16 sidecar
# through the native binder; a hand-made or generated table is never their source.
SIDECAR_ONLY = re.compile(r"e3\.|[A-Za-z0-9_.@-]*\.paired\.")
# paid-v3 (run-2) is a separate preregistered experiment, never part of F9. Its keys
# come only from the hash-checked paid-v3 numbers sidecar (BINDING-PLAN-PAIDV3 section 3),
# never from the legacy table or the audited extension table.
PAID_V3_ONLY = re.compile(r"paid_v3\.")
# Spelling variants of the same namespace (PAID_V3., paid-v3., paidv3.) are refused in the
# tables too, so a paid-v3 number cannot reach the documents under a look-alike key.
PAID_V3_LOOKALIKE = re.compile(r"paid[-_.@]*v[-_.@]*3", re.IGNORECASE)
PAID_V3_SIDECAR_SCHEMA = "paid-v3-numbers-sidecar-v1"
PAID_V3_KEY = re.compile(r"paid_v3\.[A-Za-z0-9_.@-]+")
# The sidecar generator refuses these keys too (prereg section 13: no cost or saving
# number; no collision with the F9 cross./e1./e3./paired keys). ".paired." is also
# refused right after the prefix (paid_v3.paired.x), as BINDING-PLAN section 1 rule 1 says.
PAID_V3_FORBIDDEN = re.compile(
    r"cost|price|saving|budget|usd|cny|\.paired\.|^e3\.|^cross\.|^e1\.", re.IGNORECASE
)
PAID_V3_STATUSES = {"observed", "not_evaluable"}
# Rows projected from the seal diagnostics carry that file's SHA-256 instead of the report's.
# Only these four keys come from the diagnostics (numbers_sidecar.py project()); every other
# key comes from the report, so a row cannot move between the two hashes by its source label.
PAID_V3_DIAGNOSTICS_ORIGIN = "seal-diagnostics.json#"
PAID_V3_DIAGNOSTICS_KEYS = frozenset({
    "paid_v3.e1_blocks_sealed",
    "paid_v3.units.sealed",
    "paid_v3.units.not_run",
    "paid_v3.units.not_evaluable",
})
# Values fixed by construction (BINDING-PLAN section 1 rules 4 and 5).
PAID_V3_FIXED_VALUES = {
    "paid_v3.family.primary_conclusion_available": "no",
    "paid_v3.q4.replay_bound": "0",
}
PAID_V3_ORIGIN = "paid-v3-sidecar"
HEX64 = re.compile(r"[0-9a-f]{64}")
# F10 is a separate preregistered experiment, never part of F9. Its keys come only from
# the hash-bound F10 report through f10_bindings; tables and other sources never bind
# them, including look-alike spellings (F10., f-10., f_10.).
F10_ONLY = re.compile(r"f[-_.@]*10\.", re.IGNORECASE)
F10_EXPERIMENT = "F10"
# Upper bounds checked against their same-cell ASR: cross deployments, the e1
# ablation rows of the main deployment and the F10 rows.
UPPER_BOUND_PREFIXES = ("cross.", "e1.", "f10.")


def _reject_unsealed(table: dict, origin: str) -> None:
    unsealed = sorted(key for key in table if SIDECAR_ONLY.match(key))
    if unsealed:
        raise ValueError(
            f"Sidecar-only keys in {origin}; they bind only from the sealed R16 sidecar: "
            + ", ".join(unsealed)
        )
    paid_v3 = sorted(
        key for key in table if PAID_V3_ONLY.match(key) or PAID_V3_LOOKALIKE.search(key)
    )
    if paid_v3:
        raise ValueError(
            f"paid-v3 keys in {origin}; they bind only from the hash-checked paid-v3 "
            "sidecar: " + ", ".join(paid_v3)
        )
    f10 = sorted(key for key in table if F10_ONLY.match(key))
    if f10:
        raise ValueError(
            f"F10 keys in {origin}; they bind only from the hash-bound F10 report: "
            + ", ".join(f10)
        )


def _no_duplicate_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate JSON key in the paid-v3 sidecar")
    return dict(pairs)


def paid_v3_bindings(path: Path, sidecar_sha256: str) -> dict:
    """Load paid_v3.* rows only from a binding-ready sidecar whose bytes hash to the bound value.

    The sidecar is written by the run-2 analysis tool (schema paid-v3-numbers-sidecar-v1);
    its rows keep the F9 row format (key/value/mode/denominator/source/report_sha256/status).
    Nothing is recomputed here: a row is either taken verbatim or the whole sidecar fails.
    """
    raw = path.read_bytes()
    observed = hashlib.sha256(raw).hexdigest()
    if not (
        isinstance(sidecar_sha256, str)
        and HEX64.fullmatch(sidecar_sha256)
        and observed == sidecar_sha256
    ):
        raise ValueError("paid-v3 sidecar SHA-256 differs from the bound value")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(data, dict) or data.get("schema") != PAID_V3_SIDECAR_SCHEMA:
        raise ValueError("Unsupported paid-v3 sidecar schema")
    if data.get("binding_ready") is not True or data.get("binding_blockers") != []:
        raise ValueError(
            "paid-v3 sidecar is not binding-ready (independent review GO, sealed inventory "
            "and the single analysis look are all required)"
        )
    provenance = data.get("provenance")
    report_digest = provenance.get("report_sha256") if isinstance(provenance, dict) else None
    if not (isinstance(report_digest, str) and HEX64.fullmatch(report_digest)):
        raise ValueError("paid-v3 sidecar does not name its report SHA-256")
    diagnostics_digest = provenance.get("seal_diagnostics_sha256")
    # binding_ready is the generator's own summary of its gates; a sidecar whose recorded
    # gates contradict it (no GO, no review or analysis-completion record, no seal
    # diagnostics) is refused rather than trusted on the flag alone.
    gates = data.get("gates")
    if not (
        isinstance(gates, dict)
        and gates.get("review_verdict") == "GO"
        and all(
            isinstance(gates.get(gate), dict)
            and isinstance(gates[gate].get("sha256"), str)
            and HEX64.fullmatch(gates[gate]["sha256"])
            for gate in ("independent_review", "analysis_completion")
        )
        and isinstance(diagnostics_digest, str)
        and HEX64.fullmatch(diagnostics_digest)
    ):
        raise ValueError(
            "paid-v3 sidecar gates contradict binding_ready (review GO, review and "
            "analysis-completion hashes and seal diagnostics are all required)"
        )
    rows = data.get("bindings")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("paid-v3 sidecar has no bindings")
    bindings: dict[str, dict[str, str]] = {}
    for key, row in rows.items():
        if not (
            PAID_V3_KEY.fullmatch(key)
            and not PAID_V3_FORBIDDEN.search(key.removeprefix("paid_v3."))
            and ".paired." not in key.casefold()
        ):
            raise ValueError(f"Forbidden or malformed paid-v3 key: {key}")
        fields = ("key", "value", "mode", "denominator", "source", "report_sha256", "status")
        if not isinstance(row, dict) or row.get("key") != key:
            raise ValueError(f"Malformed paid-v3 row: {key}")
        if any(not isinstance(row.get(f), str) or not row[f] or "{{" in row[f] for f in fields):
            raise ValueError(f"Missing measurement provenance: {key}")
        if (
            row["status"] not in PAID_V3_STATUSES
            or (row["status"] == "not_evaluable") != (row["value"] == "not_evaluable")
            or row["value"] == "not_run"
        ):
            raise ValueError(f"Unsupported paid-v3 row status: {key}")
        if key in PAID_V3_FIXED_VALUES and row["value"] != PAID_V3_FIXED_VALUES[key]:
            raise ValueError(f"paid-v3 value is fixed by construction: {key}")
        from_diagnostics = row["source"].startswith(PAID_V3_DIAGNOSTICS_ORIGIN)
        if from_diagnostics != (key in PAID_V3_DIAGNOSTICS_KEYS):
            raise ValueError(f"paid-v3 row source does not match its key: {key}")
        expected = diagnostics_digest if from_diagnostics else report_digest
        if row["report_sha256"] != expected:
            raise ValueError(f"paid-v3 row is not bound to the sidecar provenance: {key}")
        bindings[key] = {
            **{f: row[f] for f in fields},
            "origin": PAID_V3_ORIGIN,
            "paid_v3_sidecar_sha256": observed,
        }
    return bindings


def read_bindings(
    path: Path,
    extension: Path | None = None,
    *,
    sidecar: Path | None = None,
    sidecar_sha256: str | None = None,
    paid_v3_sidecar: Path | None = None,
    paid_v3_sidecar_sha256: str | None = None,
    f10_report: Path | None = None,
    f10_report_sha256: str | None = None,
) -> dict:
    """Merge a native report and an audited table without overriding any key."""
    if (paid_v3_sidecar is None) != (paid_v3_sidecar_sha256 is None):
        raise ValueError("A paid-v3 sidecar binds only with both its path and SHA-256")
    if (f10_report is None) != (f10_report_sha256 is None):
        raise ValueError("An F10 report binds only with both its path and SHA-256")
    native = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    if native.exists():
        from scripts.figures.report_bindings_zh import report_bindings

        bindings = report_bindings(native, sidecar, sidecar_sha256)
    elif sidecar is not None or sidecar_sha256 is not None:
        raise ValueError("A sealed sidecar binds only together with a native report")
    else:
        bindings = _read_table(path)
        _reject_unsealed(bindings, "a legacy results table")
    if extension is not None:
        extra = _read_table(extension)
        _reject_unsealed(extra, "the extension table")
        overlap = sorted(bindings.keys() & extra.keys())
        if overlap:
            raise ValueError("Key in both report and extension: " + ", ".join(overlap))
        bindings.update(extra)
    if paid_v3_sidecar is not None:
        paid_v3 = paid_v3_bindings(paid_v3_sidecar, paid_v3_sidecar_sha256)
        overlap = sorted(bindings.keys() & paid_v3.keys())
        if overlap:
            raise ValueError(
                "Key in both F9 bindings and the paid-v3 sidecar: " + ", ".join(overlap)
            )
        bindings.update(paid_v3)
    # Whatever the path (native report, sealed R16 sidecar, tables), a paid-v3 key that did
    # not come through the hash-checked paid-v3 sidecar never binds.
    stray = sorted(
        key
        for key, row in bindings.items()
        if (PAID_V3_ONLY.match(key) or PAID_V3_LOOKALIKE.search(key))
        and row.get("origin") != PAID_V3_ORIGIN
    )
    if stray:
        raise ValueError("paid-v3 keys outside the paid-v3 sidecar: " + ", ".join(stray))
    if f10_report is not None:
        from scripts.figures.report_bindings_zh import f10_bindings

        f10 = f10_bindings(f10_report, f10_report_sha256)
        overlap = sorted(bindings.keys() & f10.keys())
        if overlap:
            raise ValueError("Key in both F9 bindings and the F10 report: " + ", ".join(overlap))
        bindings.update(f10)
    stray = sorted(
        key
        for key, row in bindings.items()
        if F10_ONLY.match(key) and row.get("experiment") != F10_EXPERIMENT
    )
    if stray:
        raise ValueError("F10 keys outside the hash-bound F10 report: " + ", ".join(stray))
    for key, row in bindings.items():
        if not (key.startswith(UPPER_BOUND_PREFIXES) and key.endswith(".u95")):
            continue
        if row.get("status") in {"not_run", "not_evaluable"} and row["value"] != row["status"]:
            raise ValueError(f"Upper-bound status differs from displayed value: {key}")
        sibling = bindings.get(key.removesuffix(".u95") + ".asr", {})
        if (
            sibling.get("status") in {"not_run", "not_evaluable"}
            and row.get("status") != sibling["status"]
        ):
            raise ValueError(f"Upper-bound status differs from ASR: {key}")
        if sibling.get("status") == "unresolved" or row.get("status") == "unresolved":
            if not (sibling.get("status") == "unresolved" and row["value"] == "unknown"):
                raise ValueError(f"Unresolved cell takes u95 'unknown' and no interval: {key}")
        if (
            sibling.get("status") == "observed"
            and re.fullmatch(r"\d+/[1-9]\d*", str(sibling.get("value", "")))
            and row.get("status") != "observed"
        ):
            raise ValueError(f"Measured ASR requires its frozen interval u95: {key}")
        if row.get("status") == "observed" and not re.fullmatch(r"\d{1,3}(\.\d)?%", row["value"]):
            raise ValueError(f"Observed upper bound must be a percentage: {key}")
        counts = re.fullmatch(r"(\d+)/(\d+)", str(sibling.get("value", "")))
        if row.get("status") == "observed":
            if sibling.get("status") != "observed" or counts is None:
                raise ValueError(f"Observed u95 requires an observed ASR k/N: {key}")
            from corridor.bench.domestic_metrics import interval

            bounds = interval(int(counts[1]), int(counts[2]))
            if bounds is None or row["value"] != f"{100 * float(bounds['upper']):.1f}%":
                raise ValueError(f"u95 differs from frozen R2-7 interval: {key}")
    return bindings


def _read_table(path: Path) -> dict:
    """Read an explicit five-column table, retaining each row's provenance."""
    bindings: dict[str, dict[str, str]] = {}
    header: list[str] = []
    aliases = {
        "键": "key",
        "值": "value",
        "模式": "mode",
        "分母": "denominator",
        "来源": "source",
        "出处": "source",
    }
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.lstrip().startswith("|"):
            header = []
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        normalized = [aliases.get(cell.lower(), cell.lower()) for cell in cells]
        if {"key", "value", "mode", "denominator", "source"} <= set(normalized):
            header = normalized
            continue
        if not header or all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
            continue
        row = dict(zip(header, cells, strict=True))
        key = row["key"].removeprefix("{{NUM:").removesuffix("}}")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", key):
            raise ValueError(f"Invalid binding key at line {number}")
        if any(not row[field] for field in ("value", "mode", "denominator", "source")):
            raise ValueError(f"Missing measurement provenance: {key}")
        if (
            any("{{" in row[field] for field in ("value", "mode", "denominator", "source"))
            or key in bindings
        ):
            raise ValueError(f"Duplicate or unresolved source: {key}")
        status = "observed"
        for missing_status in ("not_run", "not_evaluable"):
            if missing_status in {row["value"], row["mode"]}:
                status = missing_status
                break
        if status == "observed" and row["value"].startswith("unknown"):
            status = "unresolved"
        if status == "observed" and re.fullmatch(r"\d+/0+", row["value"]):
            # A zero denominator has no rate or interval to report.
            status = "not_evaluable"
        bindings[key] = {
            **row,
            "status": status,
            "line": str(number),
            "report_sha256": sha(path),
        }
    if not bindings:
        raise ValueError("Final report has no explicit key/value/mode/denominator/source table")
    return bindings


def office_text(blob: bytes) -> str:
    root = ET.fromstring(blob)
    paragraphs = "\n".join(
        "".join(node.itertext())
        for node in root.iter()
        if node.tag in {f"{{{NS['w']}}}p", f"{{{NS['a']}}}p"}
    )
    extras = [
        token
        for node in root.iter()
        for fragment in [node.text or "", *node.attrib.values()]
        for token in ANY_TOKEN.findall(fragment)
    ]
    return paragraphs + "\n" + "\n".join(extras)


def is_office_temporary(path: Path) -> bool:
    return path.suffix.lower() == ".tmp" or path.name.startswith((".~lock.", "~$"))


def sources(source: Path):
    for path in sorted(source.rglob("*")):
        if (
            not path.is_file()
            or is_office_temporary(path)
            or "templates" in path.relative_to(source).parts
            or path.name == "number-consistency.json"
        ):
            continue
        if path.suffix.lower() in TEXT:
            yield path, path.read_text(encoding="utf-8-sig")
        elif path.suffix.lower() in OFFICE:
            with ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        yield path, office_text(archive.read(name))


def read_editorial(source: Path) -> dict[str, str]:
    """Read the separately human-approved editorial input, if supplied."""
    path = source / "editorial.json"
    if not path.exists():
        return {}
    editorial = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(editorial, dict) or set(editorial) - EDITORIAL_KEYS:
        raise ValueError(
            "Invalid editorial keys; only TEAM_NAME, R16_STATUS, REPO_URL and "
            "FLAGSHIP_VERDICT are allowed"
        )
    if any(
        not isinstance(value, str) or not value.strip() or UNAPPROVED_EDITORIAL.search(value)
        for value in editorial.values()
    ):
        raise ValueError(
            "Invalid editorial value; approved values must be nonempty resolved strings"
        )
    if "FLAGSHIP_VERDICT" in editorial and editorial["FLAGSHIP_VERDICT"] not in FLAGSHIP_VERDICTS:
        raise ValueError("FLAGSHIP_VERDICT must be one of the pre-approved verdict sentences")
    return editorial


def replace_editorial(text: str, editorial: dict[str, str]) -> str:
    for key, value in editorial.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def inventory(source: Path, editorial: dict[str, str] | None = None) -> dict:
    occurrences: dict[str, set[str]] = {}
    forbidden = []
    pending_editorial = []
    for path, text in sources(source):
        text = replace_editorial(text, editorial or {})
        relative = path.relative_to(source).as_posix()
        for key in TOKEN.findall(text):
            occurrences.setdefault(key, set()).add(relative)
        for token in ANY_TOKEN.findall(text):
            if token in PENDING_EDITORIAL:
                item = {"file": relative, "placeholder": token}
                if item not in pending_editorial:
                    pending_editorial.append(item)
            elif token != "{{TEAM_NAME}}" and not TOKEN.fullmatch(token):
                forbidden.append({"file": relative, "placeholder": token})
    return {
        "keys": {key: sorted(paths) for key, paths in sorted(occurrences.items())},
        "unapproved_placeholders": forbidden,
        "pending_editorial": pending_editorial,
    }


def pending_markers(source: Path) -> list[dict]:
    """Every editorial to-do marker left in the submission sources, with file and line."""
    found = []
    for path, text in sources(source):
        relative = path.relative_to(source).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            for marker in PENDING_MARKER.findall(line):
                item = {"file": relative, "line": number, "marker": marker}
                if item not in found:
                    found.append(item)
    return found


def replace_xml(
    blob: bytes, bindings: dict, editorial: dict[str, str] | None = None
) -> tuple[bytes, int]:
    # minidom retains original prefixes and namespace declarations, including
    # declarations referenced by mc:Ignorable; ElementTree would discard them.
    root = minidom.parseString(blob)
    count = 0
    for namespace in NS.values():
        for paragraph in root.getElementsByTagNameNS(namespace, "p"):
            elements = list(paragraph.getElementsByTagNameNS(namespace, "t"))
            nodes = [
                child
                for element in elements
                for child in element.childNodes
                if child.nodeType == child.TEXT_NODE
            ]
            original = "".join(node.data for node in nodes)
            replacements = {"{{NUM:" + key + "}}": row["value"] for key, row in bindings.items()}
            replacements.update({
                "{{" + key + "}}": value for key, value in (editorial or {}).items()
            })
            matches = [match for match in ANY_TOKEN.finditer(original) if match[0] in replacements]
            # Work backwards so earlier offsets remain valid, preserving all runs.
            for match in reversed(matches):
                offsets = []
                cursor = 0
                for node in nodes:
                    text = node.data
                    offsets.append((cursor, cursor + len(text), node))
                    cursor += len(text)
                value = replacements[match[0]]
                inserted = False
                for start, end, node in offsets:
                    if end <= match.start() or start >= match.end():
                        continue
                    text = node.data
                    left = text[: max(0, match.start() - start)]
                    right = text[max(0, match.end() - start) :]
                    node.data = left + (value if not inserted else "") + right
                    inserted = True
                count += 1

    # Core metadata and link attributes do not live inside Word/DrawingML
    # paragraphs. Editorial values are safe XML strings, never raw XML.
    def metadata(node):
        nonlocal count
        if node.nodeType == node.TEXT_NODE:
            updated = replace_editorial(node.data, editorial or {})
            count += int(updated != node.data)
            node.data = updated
        if node.nodeType == node.ELEMENT_NODE:
            for attribute in list(node.attributes.values()):
                updated = replace_editorial(attribute.value, editorial or {})
                count += int(updated != attribute.value)
                attribute.value = updated
        for child in node.childNodes:
            metadata(child)

    metadata(root)
    return (root.toxml(encoding="utf-8"), count) if count else (blob, 0)


def fill_tree(
    source: Path, output: Path, bindings: dict, *, allow_editorial_placeholders: bool = False
) -> dict:
    if any(row.get("dryrun") is True for row in bindings.values()):
        if not output.resolve().is_relative_to((ROOT / ".tmp").resolve()):
            raise ValueError("Dry-run reports may only be filled under the worktree .tmp directory")
    if allow_editorial_placeholders and not output.resolve().is_relative_to(
        (ROOT / ".tmp").resolve()
    ):
        raise ValueError("Editorial rehearsal outputs may only be written under the worktree .tmp")
    editorial = read_editorial(source)
    audit = inventory(source, editorial)
    if audit["unapproved_placeholders"]:
        raise ValueError("Unapproved placeholder in submission")
    if audit["pending_editorial"] and not allow_editorial_placeholders:
        raise ValueError(
            "Pending editorial decision: R16, repository URL and flagship verdict "
            "require approved sources"
        )
    missing = sorted(set(audit["keys"]) - set(bindings))
    if missing:
        raise ValueError("Missing source keys: " + ", ".join(missing))
    if output.resolve() == source.resolve() or source.resolve() in output.resolve().parents:
        raise ValueError("Output must be outside the source tree")
    if output.exists():
        raise ValueError("Output already exists; select a new revision directory")
    output.mkdir(parents=True)
    count = 0
    copied = []
    for path in sorted(source.rglob("*")):
        if (
            not path.is_file()
            or is_office_temporary(path)
            or path.name == "number-consistency.json"
        ):
            continue
        target = output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if "templates" in path.relative_to(source).parts:
            shutil.copyfile(path, target)
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT:
            content, changed = TOKEN.subn(
                lambda m: bindings[m[1]]["value"], path.read_text(encoding="utf-8-sig")
            )
            content = replace_editorial(content, editorial)
            # The public repository stores text as LF, and the exporter refuses a CR.
            target.write_text(content, encoding="utf-8", newline="\n")
            count += changed
        elif suffix in OFFICE:
            with ZipFile(path) as original, ZipFile(target, "w", ZIP_DEFLATED) as result:
                for item in original.infolist():
                    blob = original.read(item.filename)
                    if item.filename.endswith(".xml"):
                        blob, changed = replace_xml(blob, bindings, editorial)
                        count += changed
                    result.writestr(item, blob)
        elif (
            suffix not in {".png", ".jpg", ".pdf"}
            or "screenshots" in path.relative_to(source).parts
        ):
            shutil.copyfile(path, target)
        else:
            # Stale raster/PDF values must never survive a successful refill.
            copied.append(path.relative_to(source).as_posix())
    remaining = inventory(output)
    if (
        remaining["keys"]
        or remaining["unapproved_placeholders"]
        or (remaining["pending_editorial"] and not allow_editorial_placeholders)
    ):
        raise ValueError("Unresolved placeholder remains after replacement; output is invalid")
    return {
        "status": "bound_requires_render",
        "final_ready": False,
        "replacements": count,
        "bindings": {key: bindings[key] for key in audit["keys"]},
        "regenerate_artifacts": copied,
        "next": "Rebuild figures/documents/deck/video from bound sources, render and QA.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results/FINAL-RESULTS-zh.md")
    parser.add_argument(
        "--extension", type=Path, help="Audited key/value/mode/denominator/source table"
    )
    parser.add_argument("--source", type=Path, default=ROOT / "docs/submission-zh")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/filled-zh")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument(
        "--paid-v3-sidecar",
        type=Path,
        help="Binding-ready paid-v3-sidecar.json; the only source of paid_v3.* keys",
    )
    parser.add_argument(
        "--paid-v3-sidecar-sha256",
        help="Bound SHA-256 of --paid-v3-sidecar; any mismatch fails the run",
    )
    args = parser.parse_args()
    if (args.paid_v3_sidecar is None) != (args.paid_v3_sidecar_sha256 is None):
        parser.error("--paid-v3-sidecar and --paid-v3-sidecar-sha256 must be given together")
    report_path = args.report or (
        args.source / "number-consistency.json"
        if args.audit
        else args.output.parent / f"{args.output.name}-number-consistency.json"
    )
    audit = inventory(args.source)
    report = {"schema": "corridor-zh-number-bindings-v1", **audit}
    try:
        if args.audit:
            report.update(
                status="pending_results",
                final_ready=False,
                source_present=args.results.exists(),
                pending_markers=pending_markers(args.source),
            )
            code = 1 if audit["unapproved_placeholders"] else 0
        else:
            bindings = read_bindings(
                args.results,
                args.extension,
                paid_v3_sidecar=args.paid_v3_sidecar,
                paid_v3_sidecar_sha256=args.paid_v3_sidecar_sha256,
            )
            if any(row.get("dryrun") is True for row in bindings.values()) and not (
                report_path.resolve().is_relative_to((ROOT / ".tmp").resolve())
            ):
                # Never write even a failure report containing sample provenance
                # into a formal artifact directory.
                print("Dry-run binding reports may only be written under the worktree .tmp")
                return 1
            report.update(fill_tree(args.source, args.output, bindings))
            code = 0
    except (OSError, ValueError, KeyError) as error:
        report.update(status="failed", final_ready=False, error=str(error))
        code = 1
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "keys": len(audit["keys"]),
                "unapproved": len(audit["unapproved_placeholders"]),
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
