"""2026-09-16 live crash: `lbrain import` (inside an epoch build) raised FileNotFoundError when a file it had discovered
was renamed by another seat before parse() reached it (mail claims are constant traffic in source roots). A vanished or
unreadable file is skipped and named; the import completes and indexes the rest."""
from __future__ import annotations
import importlib
from click.testing import CliRunner
import lbrain.cli as cli
from lbrain.config import Config
from lbrain.store import Store


def test_vanished_file_between_discover_and_parse_is_skipped_not_fatal(tmp_path, monkeypatch):
    src = tmp_path / "root"; src.mkdir()
    (src / "keep.md").write_text("# Keep\n\nbody\n", encoding="utf-8")
    (src / "gone.md").write_text("# Gone\n\nbody\n", encoding="utf-8")
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text(f'embedding_provider = "local"\nsources = ["{src}"]\n', encoding="utf-8")
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config; importlib.reload(lbrain.config)
    real = cli.parse
    def racing_parse(path, repo_root=None):
        if path.name == "gone.md":
            path.unlink()   # the other seat moved it between discover() and parse()
            return real(path, repo_root=repo_root)
        return real(path, repo_root=repo_root)
    monkeypatch.setattr(cli, "parse", racing_parse)
    res = CliRunner().invoke(cli.main, ["import"])
    assert res.exit_code == 0, res.output
    assert "vanished before parse, skipped" in res.output and "gone.md" in res.output
    cfg = Config.load(); st = Store(cfg.db_path, cfg.embedding_dim)
    rels = {r[0] for r in st.db.execute("SELECT rel_path FROM docs")}
    st.close()
    assert any(r.endswith("keep.md") for r in rels) and not any(r.endswith("gone.md") for r in rels)
