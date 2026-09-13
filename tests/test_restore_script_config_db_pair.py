"""A5: restore must leave the config/database PAIR consistent, verified against the backup's manifest.

CCO replay 2026-09-11T07:53Z: the guide's legacy block resolved db_path from the current config, placed the
database there, then restored an older config pointing elsewhere; every command returned 0 and the search
found nothing. These tests run scripts/lbrain-backup.sh then scripts/lbrain-restore.sh and check the exact
case (changed config between backup and restore), both shapes, and a tampered manifest.
"""
import os
import shutil
import sqlite3
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP = os.path.join(ROOT, "scripts", "lbrain-backup.sh")
RESTORE = os.path.join(ROOT, "scripts", "lbrain-restore.sh")
pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None or shutil.which("bash") is None,
                                reason="needs bash and the sqlite3 binary")


def _db(path, marker):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path); c.execute("CREATE TABLE t(x TEXT)"); c.execute("INSERT INTO t VALUES (?)", (marker,)); c.commit(); c.close()


def _marker(path):
    c = sqlite3.connect(path); v = c.execute("SELECT x FROM t").fetchone()[0]; c.close(); return v


def _run(script, home, *args):
    return subprocess.run(["bash", script, *map(str, args)], env=dict(os.environ, LBRAIN_HOME=str(home)), capture_output=True, text=True)


def test_changed_config_between_backup_and_restore_is_handled(tmp_path):
    home = tmp_path / "legacy"; home.mkdir()
    orig_db = tmp_path / "store-a" / "mind.db"; _db(str(orig_db), "the-real-records")
    (home / "config.toml").write_text(f'db_path = "{orig_db}"\n')
    assert _run(BACKUP, home, tmp_path / "b").returncode == 0
    # the operator then repoints the home at a different, empty store (the CCO's exact case)
    other_db = tmp_path / "store-b" / "mind.db"; _db(str(other_db), "nothing-useful")
    (home / "config.toml").write_text(f'db_path = "{other_db}"\n')
    r = _run(RESTORE, home, tmp_path / "b")
    assert r.returncode == 0, r.stderr
    # the restored config names store-a, and the database AT THAT PATH carries the records
    cfg = (home / "config.toml").read_text()
    assert str(orig_db) in cfg and str(other_db) not in cfg
    assert _marker(str(orig_db)) == "the-real-records"
    assert "matches the backup manifest" in r.stdout


def test_epoch_home_round_trip(tmp_path):
    home = tmp_path / "home"; home.mkdir(); (home / "config.toml").write_text("x = 1\n")
    _db(str(home / "epochs" / "E1" / "brain.db"), "epoch-one"); (home / "epochs" / "CURRENT").write_text("E1\n")
    assert _run(BACKUP, home, tmp_path / "b").returncode == 0
    shutil.rmtree(home / "epochs")                      # destroy
    r = _run(RESTORE, home, tmp_path / "b")
    assert r.returncode == 0, r.stderr
    assert _marker(str(home / "epochs" / "E1" / "brain.db")) == "epoch-one"


def test_tampered_backup_fails_verification_and_keeps_the_wreck(tmp_path):
    home = tmp_path / "legacy"; home.mkdir(); (home / "config.toml").write_text("")
    _db(str(home / "brain.db"), "good")
    assert _run(BACKUP, home, tmp_path / "b").returncode == 0
    _db(str(tmp_path / "b" / "brain.db.new"), "tampered"); os.replace(tmp_path / "b" / "brain.db.new", tmp_path / "b" / "brain.db")
    r = _run(RESTORE, home, tmp_path / "b")
    assert r.returncode == 3 and "VERIFICATION FAILED" in r.stderr
    wrecks = [p for p in os.listdir(home) if p.startswith("brain.db.broken-")]
    assert wrecks and _marker(str(home / wrecks[0])) == "good"


def test_refused_backup_manifest_is_not_restored(tmp_path):
    home = tmp_path / "torn"; home.mkdir(); (home / "config.toml").write_text("")
    (home / "epochs").mkdir(); (home / "epochs" / "CURRENT").write_text("GONE\n")
    assert _run(BACKUP, home, tmp_path / "b").returncode == 2
    r = _run(RESTORE, home, tmp_path / "b")
    assert r.returncode == 2 and "refused backup" in r.stderr
