"""_write_env_var() on an EXISTING home must not rewrite the home's mode (and so its POSIX ACL mask).

Found on the workhorse, 2026-11-20T22:14Z: setting an archive passphrase on Tally's shared home ran chmod 0700 on it,
zeroed the ACL mask and cut off kraken-svc, binance-svc and two seats for about a minute. Same class as Identity.save
(fixed 2026-11-15). The secret file itself must still be 0600, and a home the call creates must still be private.
"""
import importlib
import os
import stat


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _cfg(tmp_path, monkeypatch, name):
    home = tmp_path / name
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config as config
    importlib.reload(config)
    return home, config


def test_existing_shared_home_keeps_its_mode_and_the_env_file_is_private(tmp_path, monkeypatch):
    home, config = _cfg(tmp_path, monkeypatch, "shared")
    home.mkdir()
    os.chmod(home, 0o2770)                      # a shared home: setgid, group rwx, as an operator set it
    config._write_env_var("LBRAIN_ARCHIVE_PASSPHRASE", "x" * 40)
    assert _mode(home) == 0o2770, "_write_env_var must not chmod an existing home (it resets the ACL mask)"
    assert _mode(home / "env") == 0o600


def test_a_home_the_call_creates_is_private(tmp_path, monkeypatch):
    home, config = _cfg(tmp_path, monkeypatch, "fresh")
    assert not home.exists()
    config._write_env_var("LBRAIN_ARCHIVE_PASSPHRASE", "x" * 40)
    assert _mode(home) == 0o700 and _mode(home / "env") == 0o600
