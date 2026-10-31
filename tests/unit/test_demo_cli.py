"""The demo starts only Streamlit; its gateway lives inside each local session."""

import logging

import pytest
from typer.testing import CliRunner

from corridor.cli import app

TORNADO_LOGGERS = ("tornado", "tornado.access", "tornado.application", "tornado.general")


@pytest.fixture(autouse=True)
def restored_tornado_logging():
    """Streamlit's config reload calls its ``init_tornado_logs``: it stops propagation and
    attaches a stream handler to tornado's loggers. Restore them, so a later test's
    ``caplog`` still receives the web server's access lines (R2 security review)."""
    kept = {}
    for name in TORNADO_LOGGERS:
        logger = logging.getLogger(name)
        kept[name] = (list(logger.handlers), logger.level, logger.propagate)
    try:
        yield
    finally:
        for name, (handlers, level, propagate) in kept.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def test_demo_bootstraps_local_streamlit_in_current_process(monkeypatch):
    from streamlit import config
    from streamlit.web import bootstrap

    monkeypatch.setattr(config, "_config_options", None)
    calls = []
    monkeypatch.setattr(bootstrap, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(app, ["demo", "--port", "8510"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].replace("\\", "/").endswith("corridor/ui/app.py")
    assert kwargs["flag_options"]["server.address"] == "127.0.0.1"
    assert kwargs["flag_options"]["server.port"] == 8510
    assert kwargs["flag_options"]["browser.gatherUsageStats"] is False


@pytest.mark.parametrize("headless", [False, True])
def test_demo_applies_requested_options_before_server_creation(monkeypatch, headless):
    from streamlit import config
    from streamlit.web import bootstrap

    monkeypatch.setattr(config, "_config_options", None)
    bootstrap.load_config_options({
        "server.address": "0.0.0.0",
        "server.port": 8501,
        "server.headless": not headless,
        "browser.gatherUsageStats": True,
    })
    observed = []
    expected = {
        "server.address": "127.0.0.1",
        "server.port": 8517,
        "server.headless": headless,
        "browser.gatherUsageStats": False,
    }

    def inspect_startup(*args, **kwargs):
        observed.append({key: config.get_option(key) for key in expected})

    monkeypatch.setattr(bootstrap, "run", inspect_startup)
    result = CliRunner().invoke(
        app, ["demo", "--port", "8517", "--headless" if headless else "--no-headless"]
    )
    assert result.exit_code == 0, result.output
    assert observed == [expected]
