"""A subdir import must UPDATE canonical docs, not mint phantom duplicates.

2026-08-27: `lbrain import lairs/X-STRATEGY-GTM` (a sub-path of the configured
`lairs` source) created 112 duplicate docs whose rel_paths were rooted at the
subdir ("000-.../LAIR.md") instead of canonical ("X-STRATEGY-GTM/.../LAIR.md"),
shadowing the originals. It recurred despite a known "always import from roots"
lesson, so the guard lives in the tool: import anchors rel_paths to the
configured source root that CONTAINS the requested path. A path under no
configured source is its own root (unchanged) — nothing legitimate is refused.
"""
from click.testing import CliRunner

from lbrain.cli import main
from lbrain.store import Store


def _run(home, monkeypatch, *args):
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import importlib
    import lbrain.config
    importlib.reload(lbrain.config)
    return CliRunner().invoke(main, list(args))


def _corpus(tmp_path):
    src = tmp_path / "corpus"
    (src / "sub").mkdir(parents=True)
    (src / "a.md").write_text("# A\n\nroot doc\n", encoding="utf-8")
    (src / "sub" / "b.md").write_text("# B\n\nnested doc\n", encoding="utf-8")
    return src


def _config(home, src):
    (home / "config.toml").write_text(
        f'embedding_provider = "local"\nsources = [\n  "{src}",\n]\n', encoding="utf-8"
    )


def _docs(home):
    st = Store(home / "brain.db", embedding_dim=384)
    rows = [r["rel_path"] for r in st.db.execute("SELECT rel_path FROM docs")]
    st.close()
    return rows


def test_subdir_import_updates_not_duplicates(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    src = _corpus(tmp_path)
    _config(home, src)

    # full-root import → canonical rel_paths
    _run(home, monkeypatch, "import", str(src))
    after_root = sorted(_docs(home))
    assert after_root == ["a.md", "sub/b.md"], after_root

    # subdir import of a path UNDER the configured source. Pre-fix this minted a
    # phantom "b.md" (rooted at the subdir) → 3 docs. Post-fix it anchors to the
    # configured root, so rel_path stays "sub/b.md" and the existing doc is UPDATED.
    res = _run(home, monkeypatch, "import", str(src / "sub"))
    assert res.exit_code == 0, res.output
    after_sub = sorted(_docs(home))
    assert after_sub == ["a.md", "sub/b.md"], f"phantom duplicate created: {after_sub}"
    assert "b.md" not in after_sub, "truncated phantom rel_path present"


def test_standalone_dir_not_under_source_is_its_own_root(tmp_path, monkeypatch):
    """No-regression: a path under NO configured source keeps current behavior
    (its own root) — the fix refuses nothing."""
    home = tmp_path / "h"; home.mkdir()
    src = _corpus(tmp_path)
    _config(home, src)

    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "c.md").write_text("# C\n\nstandalone\n", encoding="utf-8")

    res = _run(home, monkeypatch, "import", str(other))
    assert res.exit_code == 0, res.output
    assert "c.md" in _docs(home), _docs(home)
