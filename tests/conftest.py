"""Shared pytest plugins for every Corridor test entrypoint."""

import os
from pathlib import Path
from secrets import token_hex

import pytest

# Existing AppTests pin the evidence-complete judge view; the customer view (the app's
# default) is a new display filter covered by its own tests.
os.environ.setdefault("CORRIDOR_DEFAULT_VIEW", "judge")

pytest_plugins = ["tests._pytest_plugin"]


@pytest.fixture
def tmp_path() -> Path:
    # Windows sandbox ACLs reject tempfile's owner-only directories. Test artifacts
    # use inherited workspace ACLs and remain under the ignored workspace .tmp.
    path = Path(__file__).resolve().parents[1] / ".tmp" / ("test-" + token_hex(8))
    path.mkdir(parents=True)
    return path
