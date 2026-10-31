"""Read a file as it was at an internal freeze tag, for byte-compatibility regressions."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def frozen_source(tag: str, path: str) -> str:
    """Return ``path`` at ``tag``, or skip when this checkout does not carry the tag.

    Freeze tags exist only in the development repository. A source package has no Git
    history and the public repository starts a fresh history without them, so there the
    comparison cannot be made and the test is skipped with that reason. Where the tag is
    present, a failing ``git show`` still fails the test.
    """
    try:
        found = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--quiet", "--verify", f"refs/tags/{tag}"],
            capture_output=True,
            check=False,
        )
    except OSError:
        found = None
    if found is None or found.returncode != 0:
        pytest.skip(
            f"freeze tag {tag} is not in this checkout (source package or fresh public "
            "history); this byte-compatibility regression runs in the development repository"
        )
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{tag}:{path}"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
