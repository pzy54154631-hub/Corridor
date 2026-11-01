"""Site models do not change the six-row experiment baseline or authorization rules."""

from fractions import Fraction

import pytest

from corridor.ui.live_config import DEPLOYMENTS, SITE_DEPLOYMENTS, Credential, LiveConfig
from corridor.ui.live_runtime import LiveSession
from corridor.web import live
from corridor.web.public import DEPLOYMENTS as ALLOWED
from corridor.web.public import PublicConfig


def test_site_table_and_public_configuration():
    assert len(DEPLOYMENTS) == 6
    assert len(SITE_DEPLOYMENTS) == 11
    assert ALLOWED == frozenset({
        "deepseek-flash",
        "deepseek-v4-pro",
        "zhipu-glm-5.2",
        "zhipu-glm-5.1",
        "zhipu-glm-5-turbo",
        "zhipu-glm-4.7",
    })
    reference = next(i for i in DEPLOYMENTS if i.id == "zhipu-glm-5.3-flash")
    for model in ("glm-5.2", "glm-5.1", "glm-5-turbo", "glm-4.7"):
        site = next(i for i in SITE_DEPLOYMENTS if i.id == "zhipu-" + model)
        assert site.model == model
        assert (site.price_in, site.price_out, site.currency) == ("12", "36", "CNY")
        assert site.price_note == "live.price_note.zhipu-site"
        assert (site.provider, site.api, site.api_base, site.env_file) == (
            reference.provider,
            reference.api,
            reference.api_base,
            reference.env_file,
        )
    item = next(item for item in SITE_DEPLOYMENTS if item.id == "deepseek-v4-pro")
    assert (item.price_in, item.price_out, item.currency) == ("9", "27", "CNY")
    assert item.api_base == next(i.api_base for i in DEPLOYMENTS if i.id == "deepseek-flash")
    for allowed in (ALLOWED, frozenset({"deepseek-flash"}), frozenset({"deepseek-flash", item.id})):
        assert PublicConfig(host="demo.example", deployments=allowed).deployments == allowed
    for refused in (
        frozenset(),
        frozenset({item.id}),
        ALLOWED | {"openai-gpt-6.1-sol"},
        ALLOWED | {"zhipu-glm-5.3"},
        ALLOWED | {"zhipu-glm-5.3-flash"},
    ):
        with pytest.raises(ValueError):
            PublicConfig(host="demo.example", deployments=refused)


def test_only_present_credentials_are_listed(monkeypatch, tmp_path):
    from types import SimpleNamespace

    ws = SimpleNamespace(public=True, public_deployments=ALLOWED)
    config = LiveConfig(env_dir=tmp_path)
    monkeypatch.setattr(
        live, "read_credential", lambda p: Credential("fake") if p.name == ".env.deepseek" else None
    )
    assert [i.id for i in SITE_DEPLOYMENTS if live._listed(ws, config, i.id)] == [
        "deepseek-flash",
        "deepseek-v4-pro",
    ]
    monkeypatch.setattr(live, "read_credential", lambda p: Credential("fake"))
    assert {i.id for i in SITE_DEPLOYMENTS if live._listed(ws, config, i.id)} == ALLOWED


def test_new_site_model_uses_shared_currency_and_existing_grant(monkeypatch, tmp_path):
    # No actual credentials or model: the session's existing selection behavior stays intact.
    session = LiveSession(LiveConfig(), deployments=SITE_DEPLOYMENTS)
    session._authorized = True
    session.select("deepseek-v4-pro")
    assert session._authorized
    assert session.deployment.currency == "CNY"
    assert session._reservation(session.deployment) == Fraction(16384 * 9 + 512 * 27, 1000000)
