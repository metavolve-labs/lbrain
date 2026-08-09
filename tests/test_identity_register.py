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

    # Invalid names are refused (fresh tmp so no prior identity).
    # `Bad_Name` was here when the rule was a single lowercase label. It is a VALID
    # gcx path segment — the spec's charset is RFC 3986 `unreserved`, which includes
    # uppercase and underscore — so it moved to the accepted list below (#16).
    for bad in ("-lead", "trail-", "a" * 64, "has space", "a//b", "", "/"):
        assert runner.invoke(cli.main, ["register", "--name", bad, "--address", "0x1", "--force"]).exit_code != 0, bad


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


# --- #16: a gcx:// name is a PATH, and its case is load-bearing ------------------

def test_org_path_identities_are_accepted(tmp_path, monkeypatch):
    """The C-suite template. The old single-segment rule rejected it outright, so
    the naming scheme could not bind its own identities."""
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli
    r = CliRunner().invoke(cli.main, [
        "register", "--name", "gcx://metavolvelabs/csuite/cso/touchstone", "--address", "0x1"])
    assert r.exit_code == 0, r.output
    assert identity.Identity.load().gcx == "gcx://metavolvelabs/csuite/cso/touchstone"


def test_case_is_preserved_because_the_path_is_case_sensitive(tmp_path, monkeypatch):
    """`gcx.parse()` case-normalizes the SCHEME only, and the spec's path grammar is
    case-sensitive. The old rule lowercased the whole name, so registering
    `…/Touchstone` silently stored a DIFFERENT identifier — one that would not match
    a chain record minted with capitals. Silent corruption at the moment of binding."""
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli
    r = CliRunner().invoke(cli.main, [
        "register", "--name", "GCX://Metavolve/CSuite/Touchstone", "--address", "0x1"])
    assert r.exit_code == 0, r.output
    # scheme normalized away, path untouched
    assert identity.Identity.load().gcx == "gcx://Metavolve/CSuite/Touchstone"


def test_register_and_the_resolver_agree_on_what_is_a_name(tmp_path, monkeypatch):
    """Two validators that must accept the same strings will drift. Anything
    `register` stores must parse as a gcx:// name."""
    from lbrain import gcx
    identity = _isolate(tmp_path, monkeypatch)
    import lbrain.cli as cli
    for name in ("atlas", "metavolvelabs/csuite/cso/touchstone", "a.b_c~d", "rfc/793"):
        CliRunner().invoke(cli.main, ["register", "--name", name, "--address", "0x1", "--force"])
        stored = identity.Identity.load().gcx
        scheme, path = gcx.parse(stored)          # raises if the resolver disagrees
        assert scheme == "gcx" and path == name
