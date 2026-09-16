"""Closing fixtures for the two candidate-5b instrument holes (CSO draft, 2026-09-16, gen 197).

PROPOSAL, NOT A PATCH. Drafted against the frozen candidate-5b export pinned at bedc70d and validated in
BOTH directions by ../validate.py. The candidate owner lands these; the CSO drafts and reports.

  W2  [lbrain/cli.py]   delete the `except click.ClickException: raise` passthrough  -> suite stayed green
  W6  [lbrain/store.py] delete the realpath fallback in retire_doc_by_abs_path       -> suite stayed green

Both holes sit next to tests that already pass, which is exactly why they are holes:

  * test_r8 pins that a NON-click exception is wrapped as "failed to parse". Nothing pins what happens to a
    click exception, so deleting the passthrough only changes a case no test constructs.
  * test_r6 pins the reconcile retire when the row and the scan spell the root the SAME way. The first
    `WHERE abs_path = ?` then hits and the fallback is never consulted, so deleting it changes nothing that
    the suite looks at.

Two corrections to the CSO's own earlier framing of these cases, recorded here because the drafts below are
shaped by them (see PROPOSAL.md):

  * W2 was described as "an exception-type assertion on the 5b abort path". That cannot work. BOTH sides of
    the break raise `click.ClickException`; only the MESSAGE separates them. And the passthrough is not
    reachable from the PermissionError or double-vanish aborts: those raise from INSIDE an `except` handler,
    so they propagate out of the whole `try` statement and never meet a later `except` clause of it. The one
    reachable trigger is a ClickException raised by `parse()` itself, in the try body.
  * W6 was described as "a symlinked root renamed mid-rescan". That does not reach the fallback either:
    `discover()` yields the UNRESOLVED rglob path and `upsert_doc` stores it verbatim, so a root spelled as a
    symlink throughout writes and retires under the same string and the first query always hits. The trigger
    is a root spelled ONE way when the row was written and ANOTHER way when the reconcile retires it.
"""
from __future__ import annotations

import importlib
import os
import time

import click
import pytest
from click.testing import CliRunner

import lbrain.cli as cli
from lbrain.config import Config
from lbrain.store import Store


def _write_cfg(home, sources):
    srcs = ", ".join(f"\"{s}\"" for s in sources)
    (home / "config.toml").write_text(
        f"embedding_provider = \"local\"\nsources = [{srcs}]\n", encoding="utf-8")
    import lbrain.config
    importlib.reload(lbrain.config)


def _home(tmp_path, monkeypatch, n=2):
    src = tmp_path / "root"
    src.mkdir()
    for i in range(n):
        (src / f"doc{i}.md").write_text(f"# Doc {i}\n\nbody {i}\n", encoding="utf-8")
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    _write_cfg(home, [str(src)])
    return src, home


def _rels():
    cfg = Config.load()
    st = Store(cfg.db_path, cfg.embedding_dim)
    rels = {r[0] for r in st.db.execute("SELECT rel_path FROM docs")}
    st.close()
    return rels


# ---------- W2: a ClickException raised by parse() is an ABORT already stated ----------

W2_MSG = "index ABORTED: chunker refused doc0.md (policy CH-9)"


def test_w2_a_clickexception_from_parse_reaches_the_operator_unwrapped(tmp_path, monkeypatch):
    """GAP-5B-W2. `parse()` reaches chunking, disclosure and grading helpers, any of which may already have
    diagnosed the abort precisely and raised a `ClickException` saying so. The N5 catch-all must not relabel
    that as "failed to parse": the operator would be told the file is unparseable when the real reason -- a
    policy refusal, naming its own rule -- is one frame down and now buried inside a truncated repr.

    Both paths abort and both exit non-zero, so an exit-code or exception-TYPE assertion cannot tell them
    apart. The assertion has to be on the message the operator actually receives.
    """
    _home(tmp_path, monkeypatch)
    real = cli.parse

    def refusing(path, repo_root=None):
        if path.name == "doc0.md":
            raise click.ClickException(W2_MSG)
        return real(path, repo_root=repo_root)

    monkeypatch.setattr(cli, "parse", refusing)
    res = CliRunner().invoke(cli.main, ["import"])

    assert res.exit_code != 0, res.output
    assert f"Error: {W2_MSG}" in res.output, (
        f"the already-stated abort did not reach the operator verbatim; output was:\n{res.output}")
    assert "failed to parse" not in res.output, (
        f"a click abort was relabelled as a parse failure, hiding the rule that refused it:\n{res.output}")


def test_w2_control_a_non_click_error_is_still_wrapped_by_the_catch_all(tmp_path, monkeypatch):
    """Control for the test above: the passthrough must let click aborts through WITHOUT disarming N5. A
    guard that can only pass is not a guard, so the opposite direction is asserted too (this mirrors the
    existing test_r8 deliberately: it is the both-directions half of the same contract)."""
    _home(tmp_path, monkeypatch)
    real = cli.parse

    def broken(path, repo_root=None):
        if path.name == "doc0.md":
            raise OSError("broken symlink")
        return real(path, repo_root=repo_root)

    monkeypatch.setattr(cli, "parse", broken)
    res = CliRunner().invoke(cli.main, ["import"])

    assert res.exit_code != 0, res.output
    assert "failed to parse" in res.output and "OSError" in res.output, res.output


# ---------- W6: the row and the scan spell the root differently ----------

@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable on this platform")
def test_w6_reconcile_retires_a_row_written_under_a_different_spelling_of_the_root(tmp_path, monkeypatch):
    """GAP-5B-W6. The row was written when `sources` named the real directory; the rescan names the same
    directory through a symlink (an operator edits config.toml, a deploy swaps a `current ->` link, a home
    is reached through a bind mount). `discover()` yields the UNRESOLVED path and `upsert_doc` stores it
    verbatim, so the two spellings never match as strings and the reconcile's first lookup misses.

    Without the realpath fallback the retire silently returns zero rows and the index keeps a document at a
    path that no longer exists -- the exact failure Y1 was written to prevent, surviving under a rename of
    the root rather than of the file.

    `--no-prune` so the retire is the ONLY mechanism that could remove the row.
    """
    src, home = _home(tmp_path, monkeypatch)

    res0 = CliRunner().invoke(cli.main, ["import"])
    assert res0.exit_code == 0, res0.output
    assert any(os.path.basename(r) == "doc1.md" for r in _rels()), _rels()

    link = tmp_path / "link"
    os.symlink(src, link, target_is_directory=True)
    assert os.path.realpath(link / "doc1.md") == str(src / "doc1.md"), "fixture setup: the symlink must alias the root"
    _write_cfg(home, [str(link)])

    real = cli.parse

    def vanishing(path, repo_root=None):
        if path.name == "doc1.md" and path.exists():
            path.unlink()          # another seat removes it between discover() and parse()
        return real(path, repo_root=repo_root)

    monkeypatch.setattr(cli, "parse", vanishing)
    res = CliRunner().invoke(cli.main, ["import", "--no-prune"])

    assert res.exit_code == 0, res.output
    assert "left" in res.output and "reconcile 1/3" in res.output, (
        f"the reconcile branch never fired, so this fixture never reached the retire:\n{res.output}")
    assert "1 stale row(s) retired" in res.output, (
        f"the reconcile retired nothing: the row is spelled {src}/doc1.md and the scan watched "
        f"{link}/doc1.md vanish, so only a realpath fallback can match them:\n{res.output}")
    assert not any(os.path.basename(r) == "doc1.md" for r in _rels()), (
        f"the index still carries a row for a file that no longer exists: {_rels()}")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable on this platform")
def test_w6_unit_retire_doc_by_abs_path_matches_across_spellings_of_the_same_file(tmp_path):
    """The same contract at the unit boundary, with no CLI in the way: `retire_doc_by_abs_path` must retire
    the row of THIS FILE, however the caller spells the path to it."""
    real_dir = tmp_path / "root"
    real_dir.mkdir()
    f = real_dir / "doc1.md"
    f.write_text("# Doc 1\n\nbody\n", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(real_dir, link, target_is_directory=True)

    st = Store(tmp_path / "brain.db", 384)
    try:
        st.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) VALUES (?, ?, ?, ?, ?)",
            ("doc1.md", str(f), "Doc 1", "h" * 8, time.time()))
        st.db.commit()

        retired = st.retire_doc_by_abs_path(str(link / "doc1.md"))
        assert retired == ["doc1.md"], (
            f"retire_doc_by_abs_path returned {retired}; the row holds {f} and the caller offered "
            f"{link / 'doc1.md'}, which is the same file")
        assert [r[0] for r in st.db.execute("SELECT rel_path FROM docs")] == []
    finally:
        st.close()


def test_w6_control_an_unrelated_path_retires_nothing(tmp_path):
    """Both directions: the fallback resolves spellings of the SAME file, and must not widen the match to a
    different one. A fallback that retired on any near miss would be worse than the hole it closes."""
    real_dir = tmp_path / "root"
    real_dir.mkdir()
    f = real_dir / "doc1.md"
    f.write_text("# Doc 1\n\nbody\n", encoding="utf-8")

    st = Store(tmp_path / "brain.db", 384)
    try:
        st.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) VALUES (?, ?, ?, ?, ?)",
            ("doc1.md", str(f), "Doc 1", "h" * 8, time.time()))
        st.db.commit()

        assert st.retire_doc_by_abs_path(str(real_dir / "other.md")) == []
        assert [r[0] for r in st.db.execute("SELECT rel_path FROM docs")] == ["doc1.md"]
    finally:
        st.close()
