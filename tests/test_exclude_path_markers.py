"""Wave 0 (2026-09-05): operator-declared exclusions delist trees the source glob would
otherwise ingest. Default EMPTY (no ambient exclusion); every discover/currency/prune
site shares the predicate; Config round-trips the key."""
from pathlib import Path

from lbrain import index
from lbrain.config import Config


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "lairs"
    (src / "_archive" / "old").mkdir(parents=True)
    (src / "live").mkdir()
    (src / "_archive" / "old" / "LAIR.md").write_text("# archived\n\nold.\n", encoding="utf-8")
    (src / "live" / "LAIR.md").write_text("# live\n\nnow.\n", encoding="utf-8")
    return src


def test_default_is_empty_and_excludes_nothing(tmp_path):
    index.set_exclude_markers(())
    src = _tree(tmp_path)
    found = sorted(p.relative_to(src).as_posix() for p in index.discover([src]))
    assert found == ["_archive/old/LAIR.md", "live/LAIR.md"]


def test_marker_delists_the_tree_without_touching_disk(tmp_path):
    src = _tree(tmp_path)
    index.set_exclude_markers(["/_archive/"])
    try:
        found = sorted(p.relative_to(src).as_posix() for p in index.discover([src]))
        assert found == ["live/LAIR.md"]
        assert (src / "_archive" / "old" / "LAIR.md").exists()  # delisted, never erased
        assert index.is_excluded_path(src / "_archive" / "old" / "LAIR.md")
        assert not index.is_excluded_path(src / "live" / "LAIR.md")
    finally:
        index.set_exclude_markers(())


def test_config_round_trips_and_arms_the_predicate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import importlib
    import lbrain.config as cfgmod
    importlib.reload(cfgmod)
    cfg = cfgmod.Config(sources=[tmp_path / "lairs"], exclude_path_markers=["/_archive/", "/_archive_legacy/"])
    cfg.write()
    text = cfgmod.CONFIG_PATH.read_text(encoding="utf-8")
    assert 'exclude_path_markers = ["/_archive/", "/_archive_legacy/"]' in text
    loaded = cfgmod.Config.load()
    assert loaded.exclude_path_markers == ["/_archive/", "/_archive_legacy/"]
    assert index.exclude_markers() == ("/_archive/", "/_archive_legacy/")
    index.set_exclude_markers(())
