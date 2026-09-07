"""Renaming is not retiring (2026-09-05/06): a 000-PRIORITY- name inside a dated archive rename,
or under a retired frontmatter status, MUST NOT carry the priority boost."""
from pathlib import Path
from lbrain.index import parse


def _doc(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return parse(p, repo_root=tmp_path)


def test_live_priority_lair_is_priority(tmp_path):
    d = _doc(tmp_path, "P3/000-PRIORITY-THING/LAIR.md", "# live\n\nbody\n")
    assert d.is_priority


def test_dated_archive_rename_retires_the_name(tmp_path):
    d = _doc(tmp_path, "P3/000-PRIORITY-THING-archived-20260513/LAIR.md", "# old\n\nbody\n")
    assert not d.is_priority


def test_frontmatter_status_retires_the_name(tmp_path):
    d = _doc(tmp_path, "P3/000-PRIORITY-THING/LAIR.md", "---\nstatus: retired\n---\n# old\n\nbody\n")
    assert not d.is_priority
    d2 = _doc(tmp_path, "P3/000-PRIORITY-OTHER/LAIR.md", "---\nretired: true\n---\n# old\n\nbody\n")
    assert not d2.is_priority


def test_current_status_keeps_priority(tmp_path):
    d = _doc(tmp_path, "P3/000-PRIORITY-THING/LAIR.md", "---\nstatus: current\n---\n# live\n\nbody\n")
    assert d.is_priority
