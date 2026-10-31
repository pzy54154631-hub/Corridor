"""B3 (risk R-11): the consent boundary is an import contract.

The planner layer (corridor.agent) and the channels (corridor.channels) may not import the
simulated device signer, the wealth device or the MFA verifier: only the gateway and the
device workflow, reached through explicit gestures, may sign or verify. The import-linter
contract in pyproject.toml checks direct imports (the gateway itself uses the verifier, and
the run-task harness drives the simulated device, by design); the AST check below is the
authority, as for the trusted-core import boundary, and also catches aliased and dynamic
imports.

One pre-existing direct import is recorded as a finding and named exactly, so the contract
still fails on any new one: corridor.agent.wealth_validation (an offline M2 dialogue
validation runner that lives in the agent package) builds a WealthDevice. Moving it to
bench/ is out of scope here because bench/ is frozen.
"""

import tomllib
from pathlib import Path

from tests._ast_checks import Analysis

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "corridor"
SOURCES = ("corridor.agent", "corridor.channels")
FORBIDDEN = ("corridor.sim.device", "corridor.sim.wealth_device", "corridor.gateway.mfa")
KNOWN = {("corridor.agent.wealth_validation", "corridor.sim.wealth_device")}


def _within(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


def _violations(source, module):
    found = set()
    for reference in Analysis(source, module).imports:
        for forbidden in FORBIDDEN:
            if reference.name is None or _within(reference.name, forbidden):
                found.add((module, forbidden if reference.name else "<computed>"))
    return found


def test_the_import_linter_contract_names_the_boundary_and_only_the_known_exception():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    [contract] = [item for item in contracts if item["source_modules"] == list(SOURCES)]
    assert contract["type"] == "forbidden"
    assert contract["forbidden_modules"] == list(FORBIDDEN)
    assert contract["allow_indirect_imports"] is True
    assert contract["ignore_imports"] == [f"{left} -> {right}" for left, right in sorted(KNOWN)]


def test_agent_and_channels_never_import_the_signer_or_the_verifier():
    found = set()
    for package in ("agent", "channels"):
        for path in sorted((SOURCE / package).rglob("*.py")):
            module = ".".join(path.relative_to(SOURCE.parent).with_suffix("").parts)
            module = module.removesuffix(".__init__")
            found |= _violations(path.read_text(encoding="utf-8"), module)
    assert found == KNOWN, sorted(found)


def test_the_check_sees_aliases_relative_and_dynamic_imports():
    for source in (
        "from corridor.sim.device import Device",
        "from corridor.sim import device",
        "from ..sim import wealth_device",
        "import corridor.gateway.mfa as verifier",
        "from corridor.gateway import mfa",
        "import importlib; importlib.import_module('corridor.sim.device')",
        "import importlib; name = 'corridor.gateway.' + 'mfa'; importlib.import_module(name)",
    ):
        assert _violations(source, "corridor.agent.domestic_session"), source
    for source in (
        "from corridor.sim.keys import stable_id",
        "from corridor.gateway.service import Proposal",
        "from corridor.sim.device_workflow import DeviceWorkflow",
    ):
        assert not _violations(source, "corridor.channels.im"), source
