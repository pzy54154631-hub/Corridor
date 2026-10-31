"""Generate runtime dependency notices from the active environment's metadata."""

import argparse
import importlib.metadata
import platform
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def license_label(metadata: importlib.metadata.PackageMetadata) -> str:
    """Prefer declared expressions; never copy long licence text or author details."""
    expression = metadata.get("License-Expression", "").strip()
    if expression:
        return expression
    classifiers = sorted({
        item.rsplit(" :: ", 1)[-1]
        for item in metadata.get_all("Classifier", [])
        if item.startswith("License :: ") and item != "License :: OSI Approved"
    })
    if classifiers:
        return " / ".join(classifiers)
    label = metadata.get("License", "").strip()
    if label and len(label) <= 120 and re.fullmatch(r"[A-Za-z0-9 .()+/_-]+", label):
        if label.lower() not in {"unknown", "other", "see license", "see license.txt"}:
            return label
    return "unknown"


def runtime_distributions(requirements: list[str]) -> list[importlib.metadata.Distribution]:
    """Resolve installed runtime requirements with markers and requested extras."""
    pending = [Requirement(value) for value in requirements]
    selected: dict[str, importlib.metadata.Distribution] = {}
    expanded: dict[str, set[str]] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        distribution = importlib.metadata.distribution(name)
        selected[name] = distribution
        contexts = {"", *requirement.extras} - expanded.get(name, set())
        if not contexts:
            continue
        expanded.setdefault(name, set()).update(contexts)
        for value in distribution.requires or []:
            child = Requirement(value)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in contexts
            ):
                # The parent's extra marker has already been evaluated.
                child.marker = None
                pending.append(child)
    return [selected[name] for name in sorted(selected)]


def render() -> str:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = configuration["project"]["dependencies"]
    direct = {canonicalize_name(Requirement(value).name) for value in requirements}
    distributions = runtime_distributions(requirements)
    python = ".".join(platform.python_version_tuple()[:2])
    lines = [
        "# Third-party notices",
        "",
        "Runtime dependencies of Corridor and the licence each one declares in its package",
        "metadata. The list starts from the requirements in `pyproject.toml` and follows the",
        f"transitive requirements resolved in `uv.lock`, as installed on {platform.system()} with",
        f"Python {python}; other platforms may pull a slightly different set of conditional",
        "packages from the same lockfile. Development and build tools are not listed.",
        "",
        "Licence labels come from each package's own metadata (`License-Expression`, then",
        "licence classifiers, then the short `License` field) and are marked `unknown` when",
        "the metadata is missing or ambiguous. They are a convenience, not a substitute for",
        "the licence and notice files shipped with each package. Third-party components",
        "remain under their own terms; Corridor's evaluation-only licence does not change them.",
        "",
        "To regenerate after `uv sync --frozen`:",
        "",
        "```text",
        "uv run --frozen python scripts/public_notices.py",
        "```",
        "",
        "| Runtime dependency | Installed version | Licence metadata | Requirement |",
        "|---|---|---|---|",
    ]
    for distribution in distributions:
        name = distribution.metadata["Name"]
        relation = "direct" if canonicalize_name(name) in direct else "transitive"
        label = license_label(distribution.metadata).replace("|", "\\|")
        lines.append(f"| {name} | {distribution.version} | {label} | {relation} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "THIRD_PARTY_NOTICES.md")
    arguments = parser.parse_args()
    content = render()
    arguments.output.write_text(content, encoding="utf-8", newline="\n")
    count = sum(line.startswith("| ") for line in content.splitlines()) - 1
    print(f"Generated runtime notices: {count} packages")


if __name__ == "__main__":
    main()
