"""Fortify step 1 (A11, CSO 2026-10-15): `retired_marker_scope`.

"path" (default) keeps today's behaviour: a marker in ANY segment, the filename included,
retires the record. "dirs" counts directory segments only, so a live document whose NAME
mentions retirement is no longer buried, while a dated archive DIRECTORY still retires.
Each assertion is paired with its opposite, so the test can fail in both directions.
"""
import pytest

from lbrain import index


@pytest.fixture(autouse=True)
def _reset_scope():
    index.set_retired_marker_scope("path")
    yield
    index.set_retired_marker_scope("path")


FILE_NAMED = ["lairs", "_COLLAB", "CSO-THE-RETIRED-REGISTRY-IS-WIRED-2026-09-14.md"]
DIR_RETIRED = ["lairs", "000-PRIORITY-X-archived-20260513", "LAIR.md"]
PLAIN = ["lairs", "P3", "LAIR.md"]


def test_default_scope_is_path_and_matches_filename():
    assert index.is_retired_signal(FILE_NAMED, {}) is True
    assert index.is_retired_signal(DIR_RETIRED, {}) is True
    assert index.is_retired_signal(PLAIN, {}) is False


def test_dirs_scope_ignores_filename_but_keeps_directory_marker():
    index.set_retired_marker_scope("dirs")
    assert index.is_retired_signal(FILE_NAMED, {}) is False   # the A11 false fire is gone
    assert index.is_retired_signal(DIR_RETIRED, {}) is True   # the canary directory stays retired
    assert index.is_retired_signal(PLAIN, {}) is False


def test_dirs_scope_does_not_touch_frontmatter_routes():
    index.set_retired_marker_scope("dirs")
    assert index.is_retired_signal(PLAIN, {"status": "superseded"}) is True
    assert index.is_retired_signal(PLAIN, {"retired": True}) is True
    assert index.is_retired_signal(FILE_NAMED, {"status": "live"}) is False


@pytest.mark.parametrize("bad", [None, "", "DIRECTORY", "files"])
def test_unknown_scope_falls_back_to_path(bad):
    index.set_retired_marker_scope(bad)
    assert index.is_retired_signal(FILE_NAMED, {}) is True


def test_config_write_then_load_arms_the_scope(tmp_path, monkeypatch):
    from lbrain import config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.toml")
    cfgmod.Config(retired_marker_scope="dirs").write()
    assert 'retired_marker_scope = "dirs"' in (tmp_path / "config.toml").read_text()
    loaded = cfgmod.Config.load()
    assert loaded.retired_marker_scope == "dirs"
    assert index.is_retired_signal(FILE_NAMED, {}) is False  # load() armed the module scope
