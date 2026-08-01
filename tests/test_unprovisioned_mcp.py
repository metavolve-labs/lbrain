"""A-425 (MCP half) — an unprovisioned brain must SAY so, in-band.

The CLI half warns on stderr. No MCP client shows the model stderr, so on the
path a persona router actually uses, a typo'd LBRAIN_HOME returned `0 hits` and
`docs: 0` and the model read that as *"nothing is recorded about this"* — a
substantive negative claim sourced from an empty database.

These tests pin the distinction that matters: **absence of evidence must never
render as evidence of absence.**
"""
import importlib
import pytest


def _reload_with_home(monkeypatch, home):
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config, lbrain.mcp_server
    importlib.reload(lbrain.config)
    return importlib.reload(lbrain.mcp_server)


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    d = tmp_path / "typo"
    d.mkdir()
    return _reload_with_home(monkeypatch, d)


@pytest.fixture
def provisioned_home(tmp_path, monkeypatch):
    d = tmp_path / "real"
    d.mkdir()
    (d / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    return _reload_with_home(monkeypatch, d)


def test_banner_fires_when_no_config(empty_home):
    b = empty_home.unprovisioned_banner()
    assert "UNPROVISIONED BRAIN" in b
    # It must tell the model what NOT to conclude, not merely that a file is absent.
    assert "not an empty topic" in b
    assert "DO NOT report that nothing is known" in b


def test_banner_names_the_env_var_when_set(empty_home):
    assert "LBRAIN_HOME=" in empty_home.unprovisioned_banner()
    assert "typo" in empty_home.unprovisioned_banner()


def test_banner_silent_when_provisioned(provisioned_home):
    assert provisioned_home.unprovisioned_banner() == ""


def test_query_result_carries_the_banner(empty_home):
    out = empty_home.lair_query("what is our patent status")
    assert out.startswith("⚠️  UNPROVISIONED BRAIN")


def test_stats_result_carries_the_banner(empty_home):
    # lair_stats is the documented escape hatch for "is it really absent?" —
    # it was the one tool blind to having no brain at all.
    out = empty_home.lair_stats()
    assert "UNPROVISIONED BRAIN" in out
    assert "docs: 0" in out  # still reports the truth, just no longer bare


def test_search_result_carries_the_banner(empty_home):
    assert "UNPROVISIONED BRAIN" in empty_home.lair_search("patent")


def test_short_query_gated_path_still_warns(empty_home):
    # The AMP gate returns early. An early return is exactly where a banner
    # gets forgotten, and a gated query on a typo'd home is still a typo.
    out = empty_home.lair_query("hi")
    assert "UNPROVISIONED BRAIN" in out
