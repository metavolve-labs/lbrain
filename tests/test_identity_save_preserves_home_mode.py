"""Identity.save() on an EXISTING home must not rewrite the home's mode (and so its POSIX ACL mask).

Found by Farrier on the workhorse, 2026-11-15: shared seat homes carry group ACLs; every `register`/`save` ran
`chmod 0700` on the home and reset the ACL mask, locking the other seat out of its own brain. The private part of
this record is the identity FILE (0600 from the moment it exists), not the directory. A home that save() creates
itself is still made 0700.
"""
import os
import stat

import pytest


def _isolate(monkeypatch, home):
    import lbrain.identity as identity
    monkeypatch.setattr(identity, "IDENTITY_PATH", home / "identity.json")
    monkeypatch.setattr(identity, "CONFIG_DIR", home)
    return identity


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_existing_home_keeps_its_mode_and_the_identity_file_is_private(tmp_path, monkeypatch):
    home = tmp_path / "shared-home"
    home.mkdir()
    os.chmod(home, 0o2770)                      # a shared home: setgid, group rwx, as an operator sets it for ACLs
    assert _mode(home) == 0o2770
    identity = _isolate(monkeypatch, home)
    identity.Identity(name="shared", address="0x1", issuer="t", credentials=["domain"]).save()
    assert _mode(home) == 0o2770, "save() must not chmod an existing home (it resets the ACL mask)"
    assert _mode(home / "identity.json") == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_home_save_creates_is_private(tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    assert not home.exists()
    identity = _isolate(monkeypatch, home)
    identity.Identity(name="fresh", address="0x2", issuer="t", credentials=["domain"]).save()
    assert _mode(home) == 0o700
    assert _mode(home / "identity.json") == 0o600
