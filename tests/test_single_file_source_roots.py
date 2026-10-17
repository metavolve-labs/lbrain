"""A configured source that is a single .md FILE must index that file.

`discover()` did `root.rglob("*.md")`; on a file root rglob yields nothing, so a file
source silently indexed 0 docs. The other source walkers had the same blind spot in a
different shape: the currency survey, the import prune guard, the unreachable prune guard
and the epoch deletion manifest each asked `is_dir()` of every root, so ONE file root made
the survey call it missing, disabled every prune, and made the second epoch build refuse
with "source root VANISHED".

Contract pinned here:
  * an existing regular `.md` file root is indexed (same exclusion check, same symlink
    escape refusal and SKIPPED message as a file found under a directory root);
  * a file root that is missing, or is not .md, is reported on stderr, never silently 0;
  * directory roots are unchanged;
  * currency, prune, unreachable prune, coverage and the deletion manifest treat a present
    file root as present.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from lbrain import epoch, epoch_build, index
from lbrain.index import discover, parse

from _coldembed import seed_brain

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ---------- discover(): the defect and its controls ----------

def test_a_file_root_is_indexed(tmp_path):
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    assert discover([f]) == [f]


def test_a_file_root_gets_its_own_name_as_rel_path(tmp_path):
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    assert parse(f, repo_root=f).rel_path == "SOLO.md"


def test_directory_roots_are_unchanged(tmp_path):
    src = tmp_path / "lairs"
    _write(src / "a.md", "# A\n\nalpha\n")
    _write(src / "sub" / "b.md", "# B\n\nbeta\n")
    _write(src / "skip.txt", "not markdown\n")
    found = sorted(p.relative_to(src).as_posix() for p in discover([src]))
    assert found == ["a.md", "sub/b.md"]
    assert parse(src / "sub" / "b.md", repo_root=src).rel_path == "sub/b.md"


def test_file_and_directory_roots_mix(tmp_path):
    src = tmp_path / "lairs"
    a = _write(src / "a.md", "# A\n\nalpha\n")
    f = _write(tmp_path / "elsewhere" / "SOLO.md", "# Solo\n\nsolo\n")
    assert discover([src, f]) == [a, f]


def test_an_excluded_file_root_is_not_indexed(tmp_path):
    f = _write(tmp_path / "lairs" / "_archive" / "OLD.md", "# Old\n\nold\n")
    index.set_exclude_markers(["/_archive/"])
    try:
        assert discover([f]) == []
    finally:
        index.set_exclude_markers(())
    assert discover([f]) == [f]  # the same root with no marker IS indexed (anti-hollow)


def test_a_symlinked_file_root_is_refused(tmp_path, capsys):
    secret = _write(tmp_path / "private" / "id_rsa.md", "# key\n\nnot for the corpus\n")
    link = tmp_path / "corpus" / "notes.md"
    link.parent.mkdir()
    link.symlink_to(secret)
    assert discover([link]) == []
    err = capsys.readouterr().err
    assert re.search(r"SKIPPED .*notes\.md: resolves outside its source root", err), err
    assert "Symlinks may not escape the corpus." in err


def test_a_symlink_to_a_sibling_is_still_refused(tmp_path, capsys):
    real = _write(tmp_path / "corpus" / "real.md", "# Real\n\nreal\n")
    link = tmp_path / "corpus" / "alias.md"
    link.symlink_to(real)
    assert discover([link]) == []
    assert "SKIPPED" in capsys.readouterr().err


def test_a_file_root_under_a_symlinked_directory_is_allowed(tmp_path, capsys):
    """Control: a symlinked DIRECTORY root is allowed today; a file under one is too."""
    _write(tmp_path / "real" / "SOLO.md", "# Solo\n\nsolo\n")
    (tmp_path / "via").symlink_to(tmp_path / "real")
    f = tmp_path / "via" / "SOLO.md"
    assert discover([f]) == [f]
    assert capsys.readouterr().err == ""


def test_a_dangling_symlinked_file_root_is_refused_not_crashed(tmp_path, capsys):
    link = tmp_path / "corpus" / "gone.md"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "nowhere" / "x.md")
    assert discover([link]) == []
    assert "gone.md" in capsys.readouterr().err


def test_a_missing_file_root_warns_loudly(tmp_path, capsys):
    missing = tmp_path / "notes" / "NOT-HERE.md"
    assert discover([missing]) == []
    err = capsys.readouterr().err
    assert "NOT-HERE.md" in err and "does not exist" in err, err


def test_a_non_markdown_file_root_warns_loudly(tmp_path, capsys):
    f = _write(tmp_path / "notes" / "todo.txt", "plain text\n")
    assert discover([f]) == []
    err = capsys.readouterr().err
    assert "todo.txt" in err and ".md" in err, err


def test_a_non_regular_md_root_warns_and_is_not_read(tmp_path, capsys):
    """A FIFO named like a record would block import forever on read_text()."""
    pipe = tmp_path / "notes" / "pipe.md"
    pipe.parent.mkdir()
    os.mkfifo(pipe)
    assert discover([pipe]) == []
    err = capsys.readouterr().err
    assert "pipe.md" in err and "not a regular file" in err, err


def test_a_missing_directory_root_stays_quiet(tmp_path, capsys):
    """Control: directory-root behavior is unchanged (a missing directory is the
    unmounted-drive case the survey reports as roots_missing, not a discover warning)."""
    assert discover([tmp_path / "no-such-dir"]) == []
    assert capsys.readouterr().err == ""


def test_present_root_predicate(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = _write(tmp_path / "f.md", "# F\n")
    t = _write(tmp_path / "t.txt", "x")
    assert index.source_root_present(d) and index.source_root_present(f)
    assert not index.source_root_present(t)
    assert not index.source_root_present(tmp_path / "missing.md")
    assert not index.source_root_present(tmp_path / "missing-dir")


# ---------- the other walkers ----------

def _legacy_home(tmp_path: Path, sources: list[Path]) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'embedding_provider = "local"\n'
        f'embedding_model = "{MODEL}"\n'
        f"embedding_dim = {DIM}\n"
        f'db_path = "{home / "brain.db"}"\n'
        "sources = [\n" + "".join(f'  "{s}",\n' for s in sources) + "]\n")
    (home / "identity.json").write_text('{"who": "test-seat"}\n')
    return home


def _lbrain(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, LBRAIN_HOME=str(home))
    return subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)


def _docs(home: Path) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(home / "brain.db"))
    try:
        return sorted(con.execute("SELECT rel_path, abs_path FROM docs"))
    finally:
        con.close()


def test_import_indexes_a_file_source(tmp_path):
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    home = _legacy_home(tmp_path, [f])
    p = _lbrain(home, "import")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _docs(home) == [("SOLO.md", str(f))]


def test_currency_survey_treats_a_file_root_as_present(tmp_path):
    from lbrain import index_currency
    from lbrain.store import Store
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    home = _legacy_home(tmp_path, [f])
    assert _lbrain(home, "import").returncode == 0
    store = Store(home / "brain.db", embedding_dim=DIM)
    try:
        s = index_currency.survey(store, [f])
        assert s.roots_missing == [] and s.current == 1 and s.unchecked == 0, s
        f.write_text("# Solo\n\nEdited.\n", encoding="utf-8")
        s = index_currency.survey(store, [f])
        assert s.changed == ["SOLO.md"], s
    finally:
        store.close()


def test_import_prune_still_runs_when_one_root_is_a_file(tmp_path):
    src = tmp_path / "lairs"
    _write(src / "keep.md", "# Keep\n\nkeep\n")
    gone = _write(src / "gone.md", "# Gone\n\ngone\n")
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nsolo\n")
    home = _legacy_home(tmp_path, [src, f])
    assert _lbrain(home, "import").returncode == 0
    assert len(_docs(home)) == 3
    gone.unlink()
    p = _lbrain(home, "import")
    assert p.returncode == 0, p.stdout + p.stderr
    assert sorted(r for r, _ in _docs(home)) == ["SOLO.md", "keep.md"]


def test_prune_unreachable_still_lists_when_one_root_is_a_file(tmp_path):
    src = tmp_path / "lairs"
    _write(src / "keep.md", "# Keep\n\nkeep\n")
    other = tmp_path / "other"
    _write(other / "stray.md", "# Stray\n\nstray\n")
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nsolo\n")
    home = _legacy_home(tmp_path, [src, other, f])
    assert _lbrain(home, "import").returncode == 0
    # drop `other` from the configured sources: stray.md is now unreachable
    cfg = (home / "config.toml").read_text().replace(f'  "{other}",\n', "")
    (home / "config.toml").write_text(cfg)
    p = _lbrain(home, "prune-unreachable")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "stray.md" in p.stdout, p.stdout


def test_stale_audit_scans_a_file_source(tmp_path):
    import json
    src = tmp_path / "lairs"
    _write(src / "a.md", "# A\n\nalpha\n")
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nsolo\n")
    t = _write(tmp_path / "notes" / "todo.txt", "plain\n")
    home = _legacy_home(tmp_path, [src, f, t])
    p = _lbrain(home, "stale", "--json")
    assert p.returncode in (0, 1), p.stdout + p.stderr
    assert json.loads(p.stdout)["scanned"] == 2, p.stdout  # a.md + SOLO.md; never todo.txt


def test_epoch_builds_with_a_file_root_and_rebuilds(tmp_path):
    src = tmp_path / "lairs"
    _write(src / "a.md", "# A\n\nalpha record\n")
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    home = _legacy_home(tmp_path, [src, f])
    seed_brain(home, DIM)
    cfg = SimpleNamespace(sources=[str(src), str(f)], embedding_dim=DIM, db_path=home / "brain.db")
    r1 = epoch_build.build(home, cfg)
    assert r1["published"] and r1["docs"] == 2, r1
    db = epoch.epoch_db(home, r1["epoch_id"])
    con = sqlite3.connect(str(db))
    try:
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key='coverage_unscanned'"))
    finally:
        con.close()
    assert meta["coverage_unscanned"] == "0"  # the file root's doc is under a configured root
    # The deletion manifest used to ask is_dir() of every prior-populated root: the second
    # build refused with "source root ... VANISHED" on a present file root.
    f.write_text("# Solo\n\nOne record, revised.\n", encoding="utf-8")
    r2 = epoch_build.build(home, cfg)
    assert r2["published"] and r2["docs"] == 2 and r2["epoch_id"] != r1["epoch_id"], r2


def test_epoch_manifest_still_refuses_a_vanished_file_root(tmp_path):
    """Negative control: a file root that really vanished is still a refused deletion."""
    import pytest
    src = tmp_path / "lairs"
    _write(src / "a.md", "# A\n\nalpha record\n")
    f = _write(tmp_path / "notes" / "SOLO.md", "# Solo\n\nOne record on its own.\n")
    home = _legacy_home(tmp_path, [src, f])
    seed_brain(home, DIM)
    cfg = SimpleNamespace(sources=[str(src), str(f)], embedding_dim=DIM, db_path=home / "brain.db")
    epoch_build.build(home, cfg)
    f.unlink()
    with pytest.raises(epoch.EpochError, match="VANISHED"):
        epoch_build.build(home, cfg)
