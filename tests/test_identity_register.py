"""Outcome test for `lbrain register` — the identity WRITE command (the LBrain bind).

Tests the OUTCOME, not the mechanism: after `register`, the identity is persisted AND the
read surfaces (`Identity.load()` / `describe()`, which `whoami` + `lair_whoami` use) report the
brain AS that gcx:// identity. Isolated to a tmp path so it never touches the real ~/.lbrain.
"""
from types import SimpleNamespace

from click.testing import CliRunner


def _isolate(tmp_path, monkeypatch):
    import lbrain.identity as identity
    monkeypatch.setattr(identity, "IDENTITY_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(identity, "CONFIG_DIR", tmp_path)
    return identity


def test_register_binds_identity_and_read_surfaces_report_it(tmp_path, monkeypatch):
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli

    # Unregistered is a normal state — describe() says so before we bind.
    dummy_cfg = SimpleNamespace(db_path="", sources=[], serve_mode="structured",
                                embedding_provider="local", serve_staleness=True)
    assert identity.describe(dummy_cfg, {})["identity"]["registered"] is False

    r = CliRunner().invoke(cli.main, [
        "register", "--name", "jarvis", "--address", "0xABC123",
        "--issuer", "gcx-registrar", "--credential", "domain",
    ])
    assert r.exit_code == 0, r.output

    # OUTCOME: persisted + read back as this gcx:// identity.
    ident = identity.Identity.load()
    assert ident is not None
    assert ident.gcx == "gcx://jarvis"
    assert ident.address == "0xABC123"
    assert ident.issuer == "gcx-registrar"
    assert "domain" in ident.credentials

    info = identity.describe(dummy_cfg, {})["identity"]
    assert info["registered"] is True and info["gcx"] == "gcx://jarvis"


def test_register_normalizes_scheme_and_validates(tmp_path, monkeypatch):
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli
    runner = CliRunner()

    # A leading gcx:// is stripped; the stored label carries no scheme.
    assert runner.invoke(cli.main, ["register", "--name", "gcx://atlas", "--address", "0x1"]).exit_code == 0
    assert identity.Identity.load().gcx == "gcx://atlas"

    # Invalid labels are refused (fresh tmp so no prior identity).
    for bad in ("Bad_Name", "-lead", "trail-", "a" * 64, "has space"):
        assert runner.invoke(cli.main, ["register", "--name", bad, "--address", "0x1", "--force"]).exit_code != 0


def test_register_refuses_overwrite_without_force(tmp_path, monkeypatch):
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli
    runner = CliRunner()

    assert runner.invoke(cli.main, ["register", "--name", "first", "--address", "0x1"]).exit_code == 0
    # Second register without --force must fail and leave the first identity intact.
    r = runner.invoke(cli.main, ["register", "--name", "second", "--address", "0x2"])
    assert r.exit_code != 0
    assert identity.Identity.load().gcx == "gcx://first"
    # With --force it replaces.
    assert runner.invoke(cli.main, ["register", "--name", "second", "--address", "0x2", "--force"]).exit_code == 0
    assert identity.Identity.load().gcx == "gcx://second"
