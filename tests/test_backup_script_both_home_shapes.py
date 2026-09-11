"""A5: the documented backup route must work on BOTH home shapes the product ships.

CCO independent replay 2026-09-11T07:40Z: the guide's inline block passed on an epoch-managed home and
exited 1 on a valid legacy home (root brain.db, no epochs/), leaving a config-only backup. The route now
lives in scripts/lbrain-backup.sh and these tests RUN it against synthetic homes of each shape, a torn
epoch home, and a legacy home whose db_path is not the default. They need bash and sqlite3 on PATH.
"""
import os
import shutil
import sqlite3
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "lbrain-backup.sh")

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None or shutil.which("bash") is None,
                                reason="needs bash and the sqlite3 binary")


def _db(path, marker):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(x TEXT)"); c.execute("INSERT INTO t VALUES (?)", (marker,))
    c.commit(); c.close()


def _run(home, dest):
    env = dict(os.environ, LBRAIN_HOME=str(home))
    return subprocess.run(["bash", SCRIPT, str(dest)], env=env, capture_output=True, text=True)


def _manifest(dest):
    return open(os.path.join(dest, "BACKUP-MANIFEST.txt"), encoding="utf-8").read()


def test_epoch_home_backup_copies_tree_and_checks_current(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n')
    _db(str(home / "epochs" / "E1" / "brain.db"), "epoch-one")
    (home / "epochs" / "CURRENT").write_text("E1\n")
    r = _run(home, tmp_path / "b")
    assert r.returncode == 0, r.stderr
    m = _manifest(tmp_path / "b")
    assert "shape=epoch current=E1" in m and "quick_check=ok db=epochs/E1/brain.db" in m
    assert (tmp_path / "b" / "epochs" / "E1" / "brain.db").exists() and (tmp_path / "b" / "config.toml").exists()


def test_legacy_home_backup_copies_the_database_not_only_config(tmp_path):
    home = tmp_path / "legacy"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n')
    _db(str(home / "brain.db"), "legacy-root")
    r = _run(home, tmp_path / "b")
    assert r.returncode == 0, r.stderr
    m = _manifest(tmp_path / "b")
    assert "shape=legacy" in m and "quick_check=ok db=brain.db" in m
    c = sqlite3.connect(str(tmp_path / "b" / "brain.db"))
    assert c.execute("SELECT x FROM t").fetchone()[0] == "legacy-root"


def test_legacy_home_respects_configured_db_path(tmp_path):
    home = tmp_path / "legacy2"; home.mkdir()
    elsewhere = tmp_path / "elsewhere" / "mind.db"
    _db(str(elsewhere), "configured-path")
    (home / "config.toml").write_text(f'db_path = "{elsewhere}"\n')
    _db(str(home / "brain.db"), "decoy-at-default")   # must NOT be the one copied
    r = _run(home, tmp_path / "b")
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(tmp_path / "b" / "brain.db"))
    assert c.execute("SELECT x FROM t").fetchone()[0] == "configured-path"
    assert f"db_path={elsewhere}" in _manifest(tmp_path / "b")


def test_torn_epoch_home_is_refused_not_half_copied(tmp_path):
    home = tmp_path / "torn"; home.mkdir()
    (home / "config.toml").write_text("")
    (home / "epochs").mkdir(); (home / "epochs" / "CURRENT").write_text("GONE\n")
    r = _run(home, tmp_path / "b")
    assert r.returncode == 2 and "TORN" in r.stderr
    assert not (tmp_path / "b" / "epochs").exists()


def test_legacy_home_without_database_is_refused(tmp_path):
    home = tmp_path / "empty"; home.mkdir()
    (home / "config.toml").write_text("")
    r = _run(home, tmp_path / "b")
    assert r.returncode == 2 and "no database" in r.stderr
