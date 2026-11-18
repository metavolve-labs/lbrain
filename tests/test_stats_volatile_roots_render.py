"""Regression (CCO 2026-11-18T09:50Z): `lbrain stats` joined the volatile roots with ", ".join() after a243f8d made
them Paths, so any home with volatile_sources died at the display line with "expected str instance, PosixPath found"
and the CCO mount tail failed. The display boundary stringifies; this test holds a Path in the list."""
from __future__ import annotations

from click.testing import CliRunner


def test_stats_renders_volatile_roots_that_are_paths(tmp_path, monkeypatch):
    from lbrain import config as cfgmod
    from lbrain.cli import main as cli
    home = tmp_path / "home"; home.mkdir()
    src = tmp_path / "src"; src.mkdir(); (src / "a.md").write_text("# a\n\nalpha.\n", encoding="utf-8")
    inbox = tmp_path / "inbox"; inbox.mkdir()
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    for name in ("CONFIG_DIR", "CONFIG_PATH", "ENV_PATH", "DB_PATH"):
        if hasattr(cfgmod, name):
            val = getattr(cfgmod, name)
            monkeypatch.setattr(cfgmod, name, home / val.name if name != "CONFIG_DIR" else home, raising=False)
    (home / "config.toml").write_text(
        'embedding_provider = "local"\nembedding_model = "BAAI/bge-small-en-v1.5"\nembedding_dim = 384\n'
        f'db_path = "{home / "brain.db"}"\nsources = ["{src}", "{inbox}"]\nvolatile_sources = ["{inbox}"]\n',
        encoding="utf-8")
    res = CliRunner().invoke(cli, ["stats"])
    assert "PosixPath" not in (res.output or ""), res.output
    assert "volatile roots: 1" in res.output, res.output
