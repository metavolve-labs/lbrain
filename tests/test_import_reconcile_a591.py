"""A-591-shaped import reconcile (2026-09-16, after the CCO's STOP on the log-and-publish-rest relaxation).

A scan is COMPLETE or the build ABORTS:
  R1  a file renamed by another seat between discover() and parse(): ONE bounded re-discovery; the new path is indexed,
      the old one is absent, exit 0, the reconcile is named in the output
  R2  a file that discovery still lists but that cannot be read (twice): ABORT, non-zero exit ("unreadable is not gone")
  R3  churn: more than RECONCILE_BOUND reconciliations in one root: ABORT, non-zero exit
  R4  PermissionError: ABORT, non-zero exit
  R5  the build's response to an import abort is the EXISTING control: tests/test_epoch_build.py::
      test_interrupted_build_leaves_the_live_brain_untouched (a failed stage -> EpochError, CURRENT preserved); a non-zero
      import exit raises EpochError in _run_cli, so R2-R4 reach it by construction (pinned here by a direct assertion).
No test drops a document and calls exit 0 "acceptance": only a rename (R1) exits 0, and it must index the new path."""
from __future__ import annotations
import importlib
from click.testing import CliRunner
import lbrain.cli as cli
from lbrain import epoch_build as eb
from lbrain.config import Config
from lbrain.store import Store


def _home(tmp_path, monkeypatch, n=2):
    src = tmp_path / "root"; src.mkdir()
    for i in range(n):
        (src / f"doc{i}.md").write_text(f"# Doc {i}\n\nbody {i}\n", encoding="utf-8")
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text(f'embedding_provider = "local"\nsources = ["{src}"]\n', encoding="utf-8")
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config; importlib.reload(lbrain.config)
    return src


def _rels():
    cfg = Config.load(); st = Store(cfg.db_path, cfg.embedding_dim)
    rels = {r[0] for r in st.db.execute("SELECT rel_path FROM docs")}; st.close(); return rels


def test_r1_rename_mid_scan_is_reconciled_new_path_indexed_old_absent(tmp_path, monkeypatch):
    src = _home(tmp_path, monkeypatch)
    real = cli.parse
    def racing(path, repo_root=None):
        if path.name == "doc1.md" and path.exists():
            path.rename(path.with_name("claimed-doc1.md"))   # the other seat's claim
        return real(path, repo_root=repo_root)
    monkeypatch.setattr(cli, "parse", racing)
    res = CliRunner().invoke(cli.main, ["import"])
    assert res.exit_code == 0, res.output
    assert "reconcile 1/3" in res.output and "left" in res.output
    rels = _rels()
    assert any(r.endswith("claimed-doc1.md") for r in rels) and not any(r.endswith("doc1.md") and not r.endswith("claimed-doc1.md") for r in rels)


def test_r2_discovered_but_unreadable_twice_aborts(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    real = cli.parse
    def unreadable(path, repo_root=None):
        if path.name == "doc1.md":
            raise FileNotFoundError(str(path))   # listed by discovery, never readable
        return real(path, repo_root=repo_root)
    monkeypatch.setattr(cli, "parse", unreadable)
    res = CliRunner().invoke(cli.main, ["import"])
    assert res.exit_code != 0 and "unreadable is not gone" in res.output


def test_r3_churn_beyond_the_bound_aborts(tmp_path, monkeypatch):
    src = _home(tmp_path, monkeypatch, n=8)
    real = cli.parse
    def churn(path, repo_root=None):
        nxt = sorted(p for p in src.glob("doc*.md") if p != path)
        if nxt:
            nxt[-1].rename(nxt[-1].with_name("x-" + nxt[-1].name))   # something else moves on every parse
        return real(path, repo_root=repo_root)
    monkeypatch.setattr(cli, "parse", churn)
    # every parse renames another file, so each subsequent discovered path vanishes: reconciles exceed the bound
    res = CliRunner().invoke(cli.main, ["import"])
    assert res.exit_code != 0 and "changed under the scan more than 3 times" in res.output


def test_r4_permission_error_aborts(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    real = cli.parse
    def denied(path, repo_root=None):
        if path.name == "doc0.md":
            raise PermissionError(str(path))
        return real(path, repo_root=repo_root)
    monkeypatch.setattr(cli, "parse", denied)
    res = CliRunner().invoke(cli.main, ["import"])
    assert res.exit_code != 0 and "is not readable" in res.output


def test_r5_nonzero_import_exit_raises_epoch_error_in_the_build_runner(tmp_path):
    import pytest
    bad = tmp_path / "lbrain-bad"; bad.write_text("#!/bin/bash\necho 'import ABORTED: churn' >&2; exit 1\n"); bad.chmod(0o755)
    with pytest.raises(eb.EpochError) as ei:
        eb._run_cli(["import", "--prune"], tmp_path, str(bad))
    assert "import ABORTED" in str(ei.value)
