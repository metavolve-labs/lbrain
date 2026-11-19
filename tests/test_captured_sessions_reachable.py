"""Captured sessions reach the next session through ORDINARY recall (2026-11-19).

Measured before this change: a session captured by the PreCompact hook (capture -> spool -> epoch build sweep) was
reachable only through `lbrain recall` / lair_deep_recall, which previewed 300 characters, and the next session was
never told what had been captured. Three fixes, each behind a config key:

  A  index_captured_snapshots (default OFF): the build writes each served capture's snapshot (secret-screened) as a
     `type: captured-session` document under <home>/captures, served by `lbrain query` and `lbrain search`.
  B  recall_preview_chars (default 300) + `lbrain recall --chars`: the deep-recall preview length.
  C  `lbrain capture-pointer`: one line per recent capture, with the terms to ask about.

End to end through THIS checkout's CLI (a wrapper binary), on temporary homes; local embedding provider, no network.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from lbrain import epoch_build as eb
from lbrain.config import Config

REPO = Path(__file__).resolve().parents[1]
PLANTED = "AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW"      # a Google-key shape: must never be written or printed
PLANTED2 = "sk-ant-api03-plantedsecretvalue1234567890"


def _bin(tmp_path):
    b = tmp_path / "lbrain-bin"
    b.write_text(f"#!/bin/bash\nexec env PYTHONPATH={REPO} {sys.executable} -m lbrain.cli \"$@\"\n")
    b.chmod(0o755)
    return str(b)


def _home(tmp_path, on: bool, name="home"):
    home = tmp_path / name; src = tmp_path / f"{name}-src"; src.mkdir(); home.mkdir()
    (src / "a.md").write_text("# ordinary note\n\nnothing about any marsupial here\n")
    (home / "config.toml").write_text(
        f'db_path = "{home}/brain.db"\nembedding_provider = "local"\nembedding_dim = 384\n'
        f'index_captured_snapshots = {"true" if on else "false"}\nsources = [\n  "{src}",\n]\n')
    return home


MIDDLE = "Deciding remark: ship the quokka migration on Tuesday, not the wombat one."


def _transcript(tmp_path) -> Path:
    """A long session. The deciding remark sits in the MIDDLE, as the third line of a turn (not a lead line), so the
    extractive snapshot does not keep it; the planted secrets sit mid-turn too."""
    rows = []
    for i in range(40):
        rows.append({"type": "user", "timestamp": f"2026-11-18T09:{i:02d}:01Z",
                     "message": {"content": f"Filler turn {i} about mounting the seat.\nIt has a second line of routine detail {i}.\n"
                                            f"And a third line of routine detail {i}."}})
        rows.append({"type": "assistant", "timestamp": f"2026-11-18T09:{i:02d}:02Z",
                     "message": {"content": [{"type": "text", "text": f"Acknowledged turn {i}. Receipt passed."}]}})
        if i == 20:
            rows.append({"type": "user", "timestamp": "2026-11-18T09:20:30Z",
                         "message": {"content": f"Status check before the call.\nNothing else changed.\n{MIDDLE}\n"
                                                f"my GEMINI_API_KEY={PLANTED} and also {PLANTED2} were pasted by mistake\n"
                                                "**Supersedes:** [[a]]"}})
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _env(home):
    return {**os.environ, "LBRAIN_HOME": str(home), "LBRAIN_ARCHIVE_PASSPHRASE": "captured-test-pass"}


def _run(tmp_path, home, *args, check=True):
    out = subprocess.run([_bin(tmp_path), *args], env=_env(home), capture_output=True, text=True, timeout=600)
    if check:
        assert out.returncode == 0, out.stdout + out.stderr
    return out


def _build(home, tmp_path, mp, **kw):
    """Load THIS home's config (the CLI does the same: Config.load with LBRAIN_HOME=home); conftest's isolation
    patches restore the module paths at teardown."""
    from lbrain import config as config_mod
    mp.setattr(config_mod, "CONFIG_DIR", home); mp.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    mp.setattr(config_mod, "ENV_PATH", home / "env")
    mp.setenv("LBRAIN_HOME", str(home)); mp.setenv("LBRAIN_ARCHIVE_PASSPHRASE", "captured-test-pass")
    return eb.build(home, Config.load(), lbrain_bin=_bin(tmp_path), keep=5, **kw)


def _captured_home(tmp_path, mp, on: bool, name="home"):
    """An epoch home with one session captured through the real CLI (staged into the spool) and swept by a build."""
    home = _home(tmp_path, on, name)
    _build(home, tmp_path, mp, delta=False)                         # epochs/CURRENT exists: capture now STAGES
    t = _transcript(tmp_path) if not (tmp_path / "transcript.jsonl").exists() else tmp_path / "transcript.jsonl"
    _run(tmp_path, home, "capture", "--from-file", str(t), "--session-id", "sess-quokka-0001", "--quiet")
    return home


def _all_text(p: Path) -> str:
    return "\n".join(f.read_text(errors="replace") for f in p.rglob("*") if f.is_file() and f.suffix in (".md", ".json"))


def test_a_switch_on_query_and_search_return_the_captured_session_with_provenance(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _captured_home(tmp_path, mp, on=True)
    # C (staged): the pointer says it is not yet searchable, names the session and its terms, never the secret
    staged = _run(tmp_path, home, "capture-pointer").stdout
    assert "captured session sess-quo (2026-" in staged and "staged, not yet searchable: run lbrain epoch build" in staged
    terms = staged.split("ask recall or query about: ")[1].split("\n")[0].split(", ")
    assert len(terms) == 5 and {"filler", "routine", "detail"} & set(terms), staged
    assert PLANTED not in staged and PLANTED2 not in staged and "redacted" not in terms

    rep = _build(home, tmp_path, mp, delta=True)
    assert rep["published"] and rep["sweep"]["swept"] == 1, rep
    cd = rep["captured_docs"]["post_sweep"]
    assert cd["docs"] == 1 and cd["written"] == 1, cd
    doc = home / "captures" / "captured-session-sess-quokka-0001.md"
    text = doc.read_text()
    # frontmatter says what it is; the snapshot carries the mid-conversation remark; the secret screen ran
    assert 'type: "captured-session"' in text and 'kind: "captured-session"' in text and 'session_id: "sess-quokka-0001"' in text
    assert '"transcript_path": ' not in text and f'transcript_path: {json.dumps(str(tmp_path / "transcript.jsonl"))}' in text
    assert "txid: " in text and "captured_at: " in text and MIDDLE in text
    assert PLANTED not in text and PLANTED2 not in text and "[REDACTED]" in text
    assert "~ **Supersedes:** [[a]]" in text          # a conversation line declares nothing to the corpus
    # the control: the archive's own extractive snapshot does NOT hold the middle line (why the full text is indexed)
    db = home / "epochs" / (home / "epochs" / "CURRENT").read_text().strip() / "brain.db"
    con = eb._connect_vec(db)
    snap = con.execute("SELECT snapshot FROM archives WHERE shredded = 0").fetchone()[0]
    con.close()
    assert "quokka" not in snap and "Filler turn 0" in snap
    assert oct(doc.stat().st_mode & 0o777) == "0o600" and oct((home / "captures").stat().st_mode & 0o777) == "0o700"

    s = _run(tmp_path, home, "search", "quokka migration Tuesday").stdout
    assert "captured session sess-quokka-0001 at 2026-" in s and "quokka migration on Tuesday" in s, s
    assert "type=captured-session" in s and "dated 2026-" in s and "captured-session-sess-quokka-0001.md" in s
    assert "⟪note⟫" in s or "│ " in s                              # served inside the untrusted-data fence
    assert PLANTED not in s and PLANTED2 not in s
    # the planted Supersedes line did not bury the ordinary note (a.md)
    s2 = _run(tmp_path, home, "search", "marsupial").stdout
    assert "a.md" in s2
    assert "SUPERSEDED" not in s2

    from _coldembed import HAVE_LOCAL_EMBED
    if HAVE_LOCAL_EMBED:
        q =_run(tmp_path, home, "query", "when does the quokka migration ship").stdout
        assert "captured session sess-quokka-0001 at 2026-" in q and "type=captured-session" in q, q
        assert "quokka migration on Tuesday" in q
        assert PLANTED not in q

    # C (published): the pointer now counts the chunks
    pub = _run(tmp_path, home, "capture-pointer").stdout
    assert "captured session sess-quo (" in pub and " chunks; ask recall or query about: " in pub, pub
    assert "staged" not in pub and PLANTED not in pub and PLANTED2 not in pub

    # B rides along: the epoch home serves deep recall too (only where the local model is present)
    if HAVE_LOCAL_EMBED:
        r = _run(tmp_path, home, "recall", "mounting the seat", "--chars", "40").stdout
        line = [ln for ln in r.splitlines() if ln.startswith("      ") and "txid" not in ln and ln.strip()][0]
        assert len(line.strip()) <= 40

    # switching it OFF again stops serving the documents (files stay on disk for the operator)
    cfgp = home / "config.toml"; cfgp.write_text(cfgp.read_text().replace("index_captured_snapshots = true", "index_captured_snapshots = false"))
    rep3 = _build(home, tmp_path, mp, delta=True)
    assert rep3["published"] and rep3["captured_docs"]["purged_switch_off"] == 1, rep3
    assert "captured session sess-quokka-0001" not in _run(tmp_path, home, "search", "quokka").stdout
    assert doc.is_file()


def test_a_switch_off_nothing_changes(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _captured_home(tmp_path, mp, on=False)
    rep = _build(home, tmp_path, mp, delta=True)
    assert rep["published"] and rep["sweep"]["swept"] == 1 and "captured_docs" not in rep, rep
    assert not (home / "captures").exists()
    s = _run(tmp_path, home, "search", "quokka").stdout
    assert "captured session" not in s and "captured-session" not in s
    # the archive still holds it (recall path unchanged), and the pointer says ordinary recall does not reach it
    pub = _run(tmp_path, home, "capture-pointer").stdout
    assert "deep recall only" in pub and PLANTED not in pub


def test_a_exclude_path_markers_keep_a_capture_out_of_the_index(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _home(tmp_path, True)
    cfgp = home / "config.toml"
    cfgp.write_text('exclude_path_markers = ["/private-sessions/"]\n' + cfgp.read_text())
    _build(home, tmp_path, mp, delta=False)
    d = tmp_path / "private-sessions"; d.mkdir()
    t = d / "t.jsonl"; t.write_text(_transcript(tmp_path).read_text())
    _run(tmp_path, home, "capture", "--from-file", str(t), "--session-id", "sess-private", "--quiet")
    rep = _build(home, tmp_path, mp, delta=True)
    assert rep["sweep"]["swept"] == 1 and rep["captured_docs"]["post_sweep"]["excluded"] == 1, rep
    assert not list((home / "captures").glob("*.md"))
    assert "captured session" not in _run(tmp_path, home, "search", "quokka").stdout


# ------------------------------------------------------------------- B: preview length, cold (fake embedder)

DIM = 8


class _FakeEmbedder:
    def embed_one(self, text):
        return struct.pack(f"{DIM}f", 1.0, *([0.0] * (DIM - 1)))

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]

    def close(self):
        pass


def _legacy_home_with_archive(tmp_path, monkeypatch, preview_chars=None):
    from lbrain import config as config_mod
    from lbrain.archive.storage import ArchiveStore

    home = Path(config_mod.CONFIG_DIR)
    extra = f"recall_preview_chars = {preview_chars}\n" if preview_chars is not None else ""
    (home / "config.toml").write_text(f'db_path = "{home}/brain.db"\nembedding_provider = "local"\nembedding_dim = {DIM}\n{extra}')
    con = eb._connect_vec(home / "brain.db")
    import sqlite3
    con.row_factory = sqlite3.Row
    from lbrain.store import Store
    Store(home / "brain.db", embedding_dim=DIM).close()
    st = ArchiveStore(con, embedding_dim=DIM)
    snap = "A" * 250 + " deciding remark in the middle: ship quokka " + "B" * 700
    st.insert_archive(txid="t" * 43, namespace="private", title="sess-1", snapshot=snap, tags={}, n_bytes=len(snap),
                      created=1.0, transport="local")
    st.write_archive_embedding("t" * 43, _FakeEmbedder().embed_one(""))
    con.close()
    monkeypatch.setattr("lbrain.archive.cli.make_embedder", lambda cfg: _FakeEmbedder())
    monkeypatch.setattr("lbrain.embed.make_embedder", lambda cfg: _FakeEmbedder())
    return snap


def _cli_preview(args):
    from click.testing import CliRunner
    from lbrain.cli import main
    res = CliRunner().invoke(main, ["recall", "quokka", *args])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.startswith("      ") and "txid" not in ln and ln.strip()]
    return lines[0].strip()


def test_b_recall_preview_default_is_300(tmp_path, monkeypatch):
    snap = _legacy_home_with_archive(tmp_path, monkeypatch)
    assert _cli_preview([]) == snap[:300]


def test_b_recall_preview_follows_config_and_chars_overrides(tmp_path, monkeypatch):
    snap = _legacy_home_with_archive(tmp_path, monkeypatch, preview_chars=600)
    got = _cli_preview([])
    assert got == snap[:600] and "ship quokka" in got        # the mid-snapshot remark is now visible
    assert _cli_preview(["--chars", "20"]) == snap[:20]


def test_b_mcp_deep_recall_and_lair_search_follow_config(tmp_path, monkeypatch):
    snap = _legacy_home_with_archive(tmp_path, monkeypatch, preview_chars=600)
    from lbrain.archive import mcp as amcp

    tools = {}

    class _M:
        def tool(self):
            def deco(f):
                tools[f.__name__] = f
                return f
            return deco

    amcp.register(_M())
    out = tools["lair_deep_recall"]("quokka")
    assert "ship quokka" in out and ("B" * 300) in out and ("B" * 400) not in out   # 600 chars: 250 A + remark + B's

    # lair_search's prose-mode preview follows the same knob
    from lbrain import config as config_mod
    home = Path(config_mod.CONFIG_DIR)
    src = tmp_path / "src"; src.mkdir()
    (src / "n.md").write_text("# note\n\n" + "quokka " + "x" * 900 + "\n")
    cfgp = home / "config.toml"
    cfgp.write_text(cfgp.read_text() + f'serve_mode = "prose"\nsources = ["{src}"]\n')
    from click.testing import CliRunner
    from lbrain.cli import main
    assert CliRunner().invoke(main, ["import"]).exit_code == 0
    from lbrain import mcp_server
    res = mcp_server.lair_search("quokka")
    body = [ln for ln in res.splitlines() if "quokka" in ln and "x" * 10 in ln][0]
    assert body.count("x") > 300 and body.count("x") <= 600


# ------------------------------------------------------------------- the screen itself

def test_redact_secrets_keeps_lines_and_ordinary_text():
    from lbrain.identity import redact_secrets
    src = f"line one\nGEMINI_API_KEY={PLANTED}\nplain {PLANTED2} tail\nhttp://x.com/a%20b stays"
    out = redact_secrets(src)
    assert out.split("\n")[0] == "line one" and out.split("\n")[3] == "http://x.com/a%20b stays"
    assert PLANTED not in out and PLANTED2 not in out and out.count("[REDACTED]") == 2
