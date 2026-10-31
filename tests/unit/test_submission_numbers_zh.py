"""Reject untraceable submission measurements, including split OOXML runs."""

import ast
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.figures.fill_numbers_zh import fill_tree, inventory, read_bindings, read_editorial


def report(path: Path) -> Path:
    path.write_text(
        "| key | value | mode | denominator | source |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| e1.A5.asr | 0/24 | live-recorded | 24 cases | table E1, A5 |\n",
        encoding="utf-8",
    )
    return path


def test_missing_key_does_not_write_partial_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("{{NUM:missing}}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))
    assert not (tmp_path / "out").exists()


def test_replaces_split_ooxml_runs_and_preserves_team(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("{{TEAM_NAME}} {{NUM:e1.A5.asr}}", encoding="utf-8")
    with ZipFile(source / "deck.pptx", "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<a:p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:r><a:t>{{NUM:e1.</a:t></a:r><a:r><a:t>A5.asr}}</a:t></a:r></a:p>",
        )
    result = fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))
    assert result["replacements"] == 2
    assert (tmp_path / "out/text.md").read_text(encoding="utf-8") == "{{TEAM_NAME}} 0/24"
    with ZipFile(tmp_path / "out/deck.pptx") as archive:
        assert b"0/24" in archive.read("ppt/slides/slide1.xml")
        assert b"{{NUM:" not in archive.read("ppt/slides/slide1.xml")


@pytest.mark.parametrize(
    "row",
    [
        "| e1.A5.asr | 0/24 | | 24 | table |",
        "| e1.A5.asr | 0/24 | live-recorded | | table |",
        "| e1.A5.asr | 0/24 | live-recorded | 24 | |",
    ],
)
def test_binding_requires_provenance(tmp_path: Path, row: str) -> None:
    source = tmp_path / "r.md"
    source.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n" + row,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="provenance"):
        read_bindings(source)


def test_unapproved_placeholder_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("{{TODO}}", encoding="utf-8")
    with pytest.raises(ValueError, match="placeholder"):
        fill_tree(source, tmp_path / "out", {})


def test_r16_is_an_approved_pending_editorial_decision(tmp_path: Path) -> None:
    (tmp_path / "safety.md").write_text("R16: {{R16_STATUS}}", encoding="utf-8")
    audit = inventory(tmp_path)
    assert audit["unapproved_placeholders"] == []
    assert audit["pending_editorial"] == [{"file": "safety.md", "placeholder": "{{R16_STATUS}}"}]


def test_numeric_fill_cannot_resolve_r16_or_create_a_final_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safety.md").write_text("{{R16_STATUS}} {{NUM:e1.A5.asr}}", encoding="utf-8")
    with pytest.raises(ValueError, match="editorial decision"):
        fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))
    assert not (tmp_path / "out").exists()


def test_namespace_declarations_survive_fill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with ZipFile(source / "file.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'mc:Ignorable="w14"><w:p><w:r><w:t>{{NUM:e1.A5.asr}}</w:t></w:r></w:p></w:document>',
        )
    fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))
    with ZipFile(tmp_path / "out/file.docx") as archive:
        text = archive.read("word/document.xml")
        assert b"xmlns:w14=" in text
        assert b'mc:Ignorable="w14"' in text


def test_uneditable_office_field_fails_final_audit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with ZipFile(source / "file.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:r><w:instrText>{{NUM:e1.A5.asr}}</w:instrText></w:r></w:p>",
        )
    with pytest.raises(ValueError, match="remains"):
        fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))


def test_extension_merges_provenance_and_rejects_report_overlap(tmp_path: Path) -> None:
    original = report(tmp_path / "r.md")
    extension = tmp_path / "extension.md"
    extension.write_text(
        "| key | value | mode | denominator | source |\n"
        "|---|---|---|---|---|\n"
        "| cross.sample.A5.asr | 0/24 | audited | 24 cases | synthetic events |\n"
        "| cross.sample.A5.u95 | 11.7% | audited | 24 cases | one-sided exact bound |\n",
        encoding="utf-8",
    )
    bound = read_bindings(original, extension)
    assert set(bound) == {"e1.A5.asr", "cross.sample.A5.asr", "cross.sample.A5.u95"}
    assert bound["cross.sample.A5.u95"]["report_sha256"] != bound["e1.A5.asr"]["report_sha256"]
    with pytest.raises(ValueError, match="Key in both report and extension: e1.A5.asr"):
        read_bindings(original, original)


@pytest.mark.parametrize("status", ["not_run", "not_evaluable"])
@pytest.mark.parametrize("bound", ["20.2%", "not_run", "not_evaluable"])
def test_cross_upper_bound_requires_matching_missing_status(tmp_path, status, bound):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        f"| cross.sample.A5.asr | {status} | audited | 24 | sample |\n"
        f"| cross.sample.A5.u95 | {bound} | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    if bound == status:
        assert read_bindings(path)["cross.sample.A5.u95"]["status"] == status
    else:
        with pytest.raises(ValueError, match="Upper-bound status"):
            read_bindings(path)


@pytest.mark.parametrize("value", ["0.182892", "18.29%", "18.3", "1000%", "-1%"])
def test_cross_observed_upper_bound_requires_percentage_format(tmp_path, value):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        f"| cross.sample.A5.u95 | {value} | audited | 24 | unformatted decimal |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="percentage"):
        read_bindings(path)


def test_cross_upper_bound_coherence_is_checked_after_extension_merge(tmp_path):
    path = tmp_path / "report.md"
    extension = tmp_path / "extension.md"
    header = "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
    path.write_text(
        header + "| cross.sample.A5.asr | not_run | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    extension.write_text(
        header + "| cross.sample.A5.u95 | 20.2% | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Upper-bound status"):
        read_bindings(path, extension)


@pytest.mark.parametrize("status", ["not_run", "not_evaluable"])
def test_missing_upper_bound_cannot_keep_a_numeric_display_value(tmp_path, status):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        f"| cross.sample.A5.asr | {status} | audited | 24 | sample |\n"
        f"| cross.sample.A5.u95 | 20.2% | {status} | 24 | sample |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Upper-bound status"):
        read_bindings(path)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("upper", ["11.7%", "unknown 0.2829", "unknown"])
def test_unresolved_asr_requires_exact_unknown_upper_bound(tmp_path, native, upper):
    header = "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
    prefix = "cross.gpt-6.1-sol.A5"
    extension = tmp_path / "extension.md"
    extension.write_text(
        header + f"| {prefix}.u95 | {upper} | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    if native:
        path = tmp_path / "report.json"
        path.write_text(
            json.dumps({
                "schema_version": "domestic-report-v1",
                "dryrun": True,
                "apparatus": {},
                "summaries": [
                    {
                        "corpus": "C2",
                        "model": "gpt-6.1-sol",
                        "config": "A5",
                        "variant": "attack",
                        "mode": "scripted",
                        "cases": 24,
                        "asr_any_at_3": {
                            "events": 0,
                            "denominator": 24,
                            "unresolved_cases": 1,
                            "bounds": ["0/24", "1/24"],
                            "interval": None,
                        },
                    }
                ],
            }),
            encoding="utf-8",
        )
    else:
        path = tmp_path / "report.md"
        path.write_text(
            header + f"| {prefix}.asr | unknown [0/24, 1/24] | audited | 24 | sample |\n",
            encoding="utf-8",
        )
    if upper == "unknown":
        bound = read_bindings(path, extension)
        assert bound[f"{prefix}.asr"]["status"] == "unresolved"
        assert bound[f"{prefix}.u95"]["value"] == "unknown"
    else:
        with pytest.raises(ValueError, match="Unresolved cell"):
            read_bindings(path, extension)


@pytest.mark.parametrize("asr", ["0/24", "1/24"])
@pytest.mark.parametrize("upper", ["unknown", "unknown 0.2829"])
def test_observed_asr_cannot_take_unresolved_upper_bound(tmp_path, asr, upper):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        f"| cross.sample.A5.asr | {asr} | audited | 24 | sample |\n"
        f"| cross.sample.A5.u95 | {upper} | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unresolved cell"):
        read_bindings(path)


@pytest.mark.parametrize(
    ("asr", "upper", "accepted"),
    [
        ("0/24", "11.7%", True),
        ("1/24", "20.2%", True),
        ("1/24", "18.3%", False),  # Rejected Clopper-Pearson value, not Wilson.
        ("0/24", "20.2%", False),
        ("1/24", "11.7%", False),
    ],
)
def test_upper_bound_matches_frozen_interval_after_extension_merge(tmp_path, asr, upper, accepted):
    header = "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
    path = tmp_path / "report.md"
    path.write_text(
        header + f"| cross.sample.A5.asr | {asr} | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    extension = tmp_path / "extension.md"
    extension.write_text(
        header + f"| cross.sample.A5.u95 | {upper} | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    if accepted:
        assert read_bindings(path, extension)["cross.sample.A5.u95"]["value"] == upper
    else:
        with pytest.raises(ValueError, match="frozen R2-7 interval"):
            read_bindings(path, extension)


@pytest.mark.parametrize("asr", [None, "1 / 24"])
def test_observed_upper_bound_requires_exact_observed_asr_counts(tmp_path, asr):
    path = tmp_path / "report.md"
    header = "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
    sibling = "" if asr is None else f"| cross.sample.A5.asr | {asr} | audited | 24 | sample |\n"
    path.write_text(
        header + sibling + "| cross.sample.A5.u95 | 18.3% | audited | 24 | sample |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="observed ASR k/N"):
        read_bindings(path)


@pytest.mark.parametrize(
    ("asr", "message"),
    [
        ("2/1", "Invalid event denominator|frozen R2-7 interval"),
        # A zero denominator is not evaluable, so a numeric interval is refused.
        ("1/0", "Upper-bound status differs from ASR"),
        ("0/0", "Upper-bound status differs from ASR"),
    ],
)
def test_observed_upper_bound_rejects_invalid_or_zero_denominator(tmp_path, asr, message):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        f"| cross.sample.A5.asr | {asr} | audited | synthetic | sample |\n"
        "| cross.sample.A5.u95 | 20.2% | audited | synthetic | sample |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        read_bindings(path)


def test_zero_denominator_asr_is_not_evaluable(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(
        "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
        "| cross.sample.A5.asr | 0/0 | audited | 0 cases | sample |\n"
        "| cross.sample.A5.u95 | not_evaluable | audited | 0 cases | sample |\n",
        encoding="utf-8",
    )
    bound = read_bindings(path)
    assert bound["cross.sample.A5.asr"]["status"] == "not_evaluable"
    assert bound["cross.sample.A5.u95"]["status"] == "not_evaluable"


@pytest.mark.parametrize(
    ("asr", "upper"),
    [("1/24", "not_run"), ("0/24", "not_evaluable"), ("1/24", "not_evaluable")],
)
@pytest.mark.parametrize("split", [False, True])
def test_measured_asr_cannot_hide_its_interval_behind_a_missing_status(tmp_path, asr, upper, split):
    header = "| key | value | mode | denominator | source |\n|---|---|---|---|---|\n"
    asr_row = f"| cross.sample.A5.asr | {asr} | audited | 24 cases | sample |\n"
    upper_row = f"| cross.sample.A5.u95 | {upper} | audited | 24 cases | sample |\n"
    path = tmp_path / "report.md"
    if split:
        extension = tmp_path / "extension.md"
        path.write_text(header + asr_row, encoding="utf-8")
        extension.write_text(header + upper_row, encoding="utf-8")
    else:
        extension = None
        path.write_text(header + asr_row + upper_row, encoding="utf-8")
    with pytest.raises(ValueError, match="Measured ASR requires its frozen interval u95"):
        read_bindings(path, extension)


def test_approved_editorial_binds_text_split_runs_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    editorial = {
        "TEAM_NAME": "Demo Team",
        "R16_STATUS": "reviewed",
        "REPO_URL": "https://example.invalid/repo",
    }
    (source / "editorial.json").write_text(json.dumps(editorial), encoding="utf-8")
    (source / "text.md").write_text("{{TEAM_NAME}} {{R16_STATUS}} {{REPO_URL}}", encoding="utf-8")
    with ZipFile(source / "file.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:r><w:t>{{R16_</w:t></w:r><w:r><w:t>STATUS}} {{REPO_</w:t></w:r>"
            "<w:r><w:t>URL}}</w:t></w:r></w:p>",
        )
        archive.writestr("docProps/core.xml", "<author>{{TEAM_NAME}}</author>")
    fill_tree(source, tmp_path / "out", {})
    assert (tmp_path / "out/text.md").read_text(encoding="utf-8") == " ".join(editorial.values())
    with ZipFile(tmp_path / "out/file.docx") as archive:
        assert b"{{" not in archive.read("word/document.xml")
        assert b"Demo Team" in archive.read("docProps/core.xml")
    assert inventory(tmp_path / "out")["pending_editorial"] == []


def test_editorial_cannot_resolve_unapproved_tokens(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "editorial.json").write_text('{"UNAPPROVED": "yes"}', encoding="utf-8")
    (source / "text.md").write_text("{{UNAPPROVED}}", encoding="utf-8")
    with pytest.raises(ValueError, match="editorial"):
        fill_tree(source, tmp_path / "out", {})
    assert not (tmp_path / "out").exists()


def test_repo_url_is_pending_and_cannot_be_silently_filled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("{{REPO_URL}}", encoding="utf-8")
    assert inventory(source)["pending_editorial"] == [
        {"file": "text.md", "placeholder": "{{REPO_URL}}"}
    ]
    assert inventory(source)["unapproved_placeholders"] == []
    with pytest.raises(ValueError, match="editorial decision"):
        fill_tree(source, tmp_path / "out", {})


def test_flagship_verdict_is_a_pending_slot_until_an_approved_sentence_is_chosen(
    tmp_path: Path,
) -> None:
    from scripts.figures.fill_numbers_zh import FLAGSHIP_VERDICTS

    source = tmp_path / "source"
    source.mkdir()
    (source / "script.md").write_text("回放后。{{FLAGSHIP_VERDICT}}授权不能漂。", encoding="utf-8")
    audit = inventory(source)
    assert audit["unapproved_placeholders"] == []
    assert audit["pending_editorial"] == [
        {"file": "script.md", "placeholder": "{{FLAGSHIP_VERDICT}}"}
    ]
    with pytest.raises(ValueError, match="editorial decision"):
        fill_tree(source, tmp_path / "unfilled", {})
    assert not (tmp_path / "unfilled").exists()
    (source / "editorial.json").write_text(
        json.dumps({"FLAGSHIP_VERDICT": "两侧完全一致。"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="pre-approved verdict"):
        fill_tree(source, tmp_path / "invented", {})
    assert not (tmp_path / "invented").exists()
    for index, verdict in enumerate(FLAGSHIP_VERDICTS):
        (source / "editorial.json").write_text(
            json.dumps({"FLAGSHIP_VERDICT": verdict}, ensure_ascii=False), encoding="utf-8"
        )
        fill_tree(source, tmp_path / f"out{index}", {})
        assert (tmp_path / f"out{index}/script.md").read_text(encoding="utf-8") == (
            f"回放后。{verdict}授权不能漂。"
        )


def test_flagship_verdict_sentences_match_the_narration_fallback() -> None:
    from scripts.figures.fill_numbers_zh import FLAGSHIP_VERDICTS

    root = Path(__file__).resolve().parents[2]
    if not (root / "scripts/demo/narration_zh.json").is_file():
        pytest.skip("the video narration script is not distributed with the source package")
    narration = json.loads((root / "scripts/demo/narration_zh.json").read_text(encoding="utf-8"))
    closing = narration["segments"][-1]
    assert "{{FLAGSHIP_VERDICT}}" in closing["text"]
    assert tuple(closing["fallback"]["approved_verdicts"]) == FLAGSHIP_VERDICTS


def test_editorial_rehearsal_requires_isolated_output(tmp_path: Path, monkeypatch) -> None:
    from scripts.figures import fill_numbers_zh

    monkeypatch.setattr(fill_numbers_zh, "ROOT", tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("{{R16_STATUS}}", encoding="utf-8")
    with pytest.raises(ValueError, match="rehearsal"):
        fill_tree(source, tmp_path / "out", {}, allow_editorial_placeholders=True)
    result = fill_tree(source, tmp_path / ".tmp/out", {}, allow_editorial_placeholders=True)
    assert result["final_ready"] is False
    assert inventory(tmp_path / ".tmp/out")["pending_editorial"]


def test_refill_drops_stale_results_but_retains_screenshot_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    (source / "screenshots").mkdir()
    (source / "report.pdf").write_bytes(b"stale document")
    (source / "figures/results.png").write_bytes(b"stale result image")
    (source / "screenshots/card.png").write_bytes(b"retained evidence")
    result = fill_tree(source, tmp_path / "out", {})
    assert sorted(result["regenerate_artifacts"]) == ["figures/results.png", "report.pdf"]
    assert not (tmp_path / "out/report.pdf").exists()
    assert not (tmp_path / "out/figures/results.png").exists()
    assert (tmp_path / "out/screenshots/card.png").read_bytes() == b"retained evidence"


def test_filled_text_is_lf_whatever_the_source_or_the_platform(tmp_path: Path) -> None:
    # The public repository stores text as LF and the exporter refuses a CR, so a filled
    # document must not carry one on Windows, nor keep one from a CRLF source.
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_bytes("一\r\n{{NUM:e1.A5.asr}}\r\n三\n".encode())
    (source / "editorial.json").write_bytes(b'{\r\n  "TEAM_NAME": "Corridor"\r\n}\r\n')
    fill_tree(source, tmp_path / "out", read_bindings(report(tmp_path / "r.md")))
    assert (tmp_path / "out/text.md").read_bytes() == "一\n0/24\n三\n".encode()
    assert (tmp_path / "out/editorial.json").read_bytes() == (b'{\n  "TEAM_NAME": "Corridor"\n}\n')


@pytest.mark.parametrize("name", ["fill_numbers_zh.py", "rebuild_all_zh.py"])
def test_every_text_write_of_the_fill_names_its_newline(name: str) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts/figures" / name
    if not path.is_file():
        pytest.skip("the rebuild driver is not distributed with the source package")
    calls = [
        node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    ]
    assert calls, name
    for call in calls:
        newline = [item.value for item in call.keywords if item.arg == "newline"]
        assert [getattr(value, "value", None) for value in newline] == ["\n"], call.lineno


@pytest.mark.parametrize(
    "value",
    [
        "DRYRUN-TEAM",
        "dryrun-team",
        "DryRun 占位",
        "DRYRUN-R16_STATUS-PENDING",
        "结论待定 PENDING",
        "{{R16_STATUS}}",
        "还有 {{ 未解析",
    ],
)
@pytest.mark.parametrize("key", ["TEAM_NAME", "R16_STATUS", "REPO_URL"])
def test_editorial_refuses_rehearsal_markers_and_unresolved_slots(
    key: str, value: str, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "text.md").write_text("正文", encoding="utf-8")
    (source / "editorial.json").write_text(
        json.dumps({key: value}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Invalid editorial value"):
        read_editorial(source)
    with pytest.raises(ValueError, match="Invalid editorial value"):
        fill_tree(source, tmp_path / "out", {})
    assert not (tmp_path / "out").exists()


def test_editorial_keeps_approved_values_with_the_lowercase_word(tmp_path: Path) -> None:
    # Positive calibration: PENDING is the marker's upper-case spelling, not the English word.
    source = tmp_path / "source"
    source.mkdir()
    editorial = {
        "TEAM_NAME": "Corridor",
        "R16_STATUS": "the rule stopped the partition; nothing is pending",
        "REPO_URL": "https://example.invalid/repo",
    }
    (source / "editorial.json").write_text(json.dumps(editorial), encoding="utf-8")
    assert read_editorial(source) == editorial
