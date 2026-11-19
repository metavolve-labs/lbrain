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
                                                "**Supersedes:** [[a]]\n"
                                                "as of 2020-01-01 the quokka plan stood; see [[a]] for the background"}})
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
    # condition 4: the captured document mints NO graph edges (the conversation quoted [[a]] twice)
    con = eb._connect_vec(db)
    rel = con.execute("SELECT rel_path FROM docs WHERE abs_path = ?", (str(doc),)).fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM wikilinks WHERE src_path = ?", (rel,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM supersessions WHERE src_path = ?", (rel,)).fetchone()[0] == 0
    con.close()
    # ... and the raw-text date ladder (`lbrain stale`) dates it by its capture, not by "as of 2020-01-01"
    from lbrain.staleness import claim_date
    label, when = claim_date(text, doc.name, "")
    assert label == "dated" and when.startswith("2026-") and when != "2020-01-01"

    s = _run(tmp_path, home, "search", "quokka migration Tuesday").stdout
    assert "captured session sess-quokka-0001 at 2026-" in s and "quokka migration on Tuesday" in s, s
    assert "type=captured-session" in s and "dated 2026-" in s and "captured-session-sess-quokka-0001.md" in s
    assert "⟪note⟫" in s or "│ " in s                              # served inside the untrusted-data fence
    assert PLANTED not in s and PLANTED2 not in s
    # condition 4: the chunk that SAYS "as of 2020-01-01" is still served dated by its capture time
    s_asof = _run(tmp_path, home, "search", "quokka plan stood").stdout
    assert "captured session sess-quokka-0001" in s_asof and "dated 2026-" in s_asof, s_asof
    assert "as-of 2020-01-01" not in s_asof and "2020-01-01 ·" not in s_asof
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

    # switching it OFF again stops serving the documents AND deletes them from disk (CSO defect 2); the report
    # names what remains under the root (nothing here: no shred happened, so no tombstones)
    cfgp = home / "config.toml"; cfgp.write_text(cfgp.read_text().replace("index_captured_snapshots = true", "index_captured_snapshots = false"))
    rep3 = _build(home, tmp_path, mp, delta=True)
    cd3 = rep3["captured_docs"]
    assert rep3["published"] and cd3["purged_switch_off"] == 1 and cd3["files_deleted_switch_off"] == 1, rep3
    assert cd3["remaining_under_root"] == []
    assert "captured session sess-quokka-0001" not in _run(tmp_path, home, "search", "quokka").stdout
    assert not doc.exists()


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


# ------------------------------------------------------------------- CSO security read of 8c706b9 (2026-11-19)

def _stage_without_path(home, sid, text):
    """A spool entry exactly as every capture staged BEFORE this change looks: no `source_path` in its meta."""
    from lbrain.spool import META_SUFFIX, PAYLOAD_SUFFIX, staging_dir
    d = staging_dir(home); d.mkdir(parents=True, exist_ok=True)
    payload = text.encode(); h = hashlib.sha256(payload).hexdigest(); stem = h[:32]
    (d / f"{stem}{PAYLOAD_SUFFIX}").write_bytes(payload)
    (d / f"{stem}{META_SUFFIX}").write_text(json.dumps({"schema": 1, "sha256": h, "size": len(payload), "session_id": sid,
                                                        "title": sid, "namespace": None, "captured_at": "2026-11-18T09:00:00Z"}))


def test_defect1_capture_without_recorded_path_is_skipped_unless_backfill_flag(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _home(tmp_path, True)
    _build(home, tmp_path, mp, delta=False)
    _stage_without_path(home, "sess-old-0001", _transcript(tmp_path).read_text())
    rep = _build(home, tmp_path, mp, delta=True)
    cd = rep["captured_docs"]["post_sweep"]
    assert rep["sweep"]["swept"] == 1 and cd["skipped_no_path"] == 1 and cd["docs"] == 0 and "backfilled" not in cd, cd
    assert not list((home / "captures").glob("*.md"))
    assert "captured session" not in _run(tmp_path, home, "search", "quokka").stdout
    # CSO 04:02Z: no backfill without naming the sessions expected -- the bare flag and an empty list refuse
    bare = _run(tmp_path, home, "epoch", "build", "--backfill-captures-without-path", check=False)
    assert bare.returncode == 2, bare.stdout + bare.stderr
    empty = _run(tmp_path, home, "epoch", "build", "--backfill-captures-without-path", " , ", check=False)
    assert empty.returncode == 2 and "needs the sessions you expect" in empty.stdout + empty.stderr
    # a list that does NOT name the capture found: reported, not indexed; the named-but-absent session is reported
    rep = _build(home, tmp_path, mp, delta=True, backfill_captures_without_path=("sess-other-9999",))
    cd = rep["captured_docs"]
    assert cd["post_sweep"]["not_on_backfill_list"][0]["session_id"] == "sess-old-0001", cd
    assert cd["backfill_expected_not_found"] == ["sess-other-9999"] and "backfilled" not in cd["post_sweep"], cd
    assert not list((home / "captures").glob("*.md"))
    # the list that names it: indexed, and listed as backfilled in the build report
    rep = _build(home, tmp_path, mp, delta=True, backfill_captures_without_path=("sess-old-0001",))
    cd = rep["captured_docs"]
    assert cd["post_sweep"]["backfilled"][0]["session_id"] == "sess-old-0001" and "backfill_expected_not_found" not in cd
    assert (home / "captures" / "captured-session-sess-old-0001.md").is_file()
    assert "captured session sess-old-0001" in _run(tmp_path, home, "search", "quokka migration Tuesday").stdout
    # a later ordinary build keeps the operator's backfill (it is not re-judged as "not on the list")
    rep = _build(home, tmp_path, mp, delta=True)
    assert rep["captured_docs"]["post_sweep"]["kept_backfilled"] == 1
    assert (home / "captures" / "captured-session-sess-old-0001.md").is_file()
    # the CLI's @FILE form (one id per line, '#' comments) through a real build, where the local model is present
    from _coldembed import HAVE_LOCAL_EMBED
    if HAVE_LOCAL_EMBED:
        lst = tmp_path / "expected.txt"; lst.write_text("# sessions I expect\nsess-old-0001\nsess-other-9999\n")
        out = _run(tmp_path, home, "epoch", "build", "--backfill-captures-without-path", f"@{lst}").stdout
        assert '"backfill_expected": ["sess-old-0001", "sess-other-9999"]' in out and \
            '"backfill_expected_not_found": ["sess-other-9999"]' in out, out


def test_defect2_shred_unlinks_the_twin_in_the_same_call_and_the_next_build_drops_its_rows(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _captured_home(tmp_path, mp, on=True)
    _build(home, tmp_path, mp, delta=True)
    doc = home / "captures" / "captured-session-sess-quokka-0001.md"
    txid = json.loads(doc.read_text().split("\ntxid: ", 1)[1].split("\n", 1)[0])
    assert "captured session sess-quokka-0001" in _run(tmp_path, home, "search", "quokka migration Tuesday").stdout
    out = _run(tmp_path, home, "shred", "--txid", txid, "--yes", check=False)
    text = out.stdout + out.stderr
    # the epoch home still refuses the KEY shred (non-zero, unchanged); the plaintext twin is gone in this very call,
    # and the command SAYS so (a), says what remains, and says how to finish (b) -- CSO ruling 04:02Z
    assert out.returncode != 0, text
    assert f"plaintext captured-session document for {txid} removed for good: {doc}" in text, text
    assert "The encrypted record, its key and its `lbrain recall` snapshot REMAIN: the archive key shred is refused " \
           "on an epoch home." in text
    route = text.split("to complete the shred on this epoch home, run: ", 1)[1].split("  (", 1)[0]
    assert route.endswith("lbrain epoch build") and f"shred -u {home / 'keys' / (txid + '.key')}" in route, route
    assert "another served capture" not in text                     # this session has only the one capture
    assert text.index("removed for good") < text.index("to complete the shred")
    assert not doc.exists() and (home / "captures" / ".shredded" / txid).is_file()
    rep = _build(home, tmp_path, mp, delta=True)
    cd = rep["captured_docs"]
    assert cd["index_rows_dropped"] == ["captured-session-sess-quokka-0001.md"], cd
    assert cd["post_sweep"]["shredded"] == 1 and cd["post_sweep"]["written"] == 0, cd   # never written back
    assert not doc.exists()
    assert "captured session sess-quokka-0001" not in _run(tmp_path, home, "search", "quokka migration Tuesday").stdout
    # (b) is the real route: run its file steps, then the build -> the record is shredded in the new epoch
    steps = [s for s in route.split(" && ") if s != "lbrain epoch build"]
    for s in steps:
        assert s.startswith("shred -u "), s
        subprocess.run(["bash", "-c", s], check=True)
    rep2 = _build(home, tmp_path, mp, delta=True)
    assert rep2["sweep"]["healed_keyless"] == 1, rep2["sweep"]
    db = home / "epochs" / (home / "epochs" / "CURRENT").read_text().strip() / "brain.db"
    con = eb._connect_vec(db)
    row = con.execute("SELECT shredded, snapshot FROM archives WHERE txid = ?", (txid,)).fetchone()
    con.close()
    assert row[0] == 1 and row[1] == "", row
    rt = _run(tmp_path, home, "retrieve", "--txid", txid, "--out", str(tmp_path / "x.bin"), check=False)
    assert rt.returncode != 0                                         # undecryptable: the key is gone


def test_shred_names_another_served_capture_of_the_same_session(tmp_path, monkeypatch):
    mp = monkeypatch
    home = _captured_home(tmp_path, mp, on=True)
    _build(home, tmp_path, mp, delta=True)
    old = home / "captures" / "captured-session-sess-quokka-0001.md"
    old_txid = json.loads(old.read_text().split("\ntxid: ", 1)[1].split("\n", 1)[0])
    longer = tmp_path / "transcript2.jsonl"
    longer.write_text((tmp_path / "transcript.jsonl").read_text() + json.dumps(
        {"type": "user", "timestamp": "2026-11-18T10:00:00Z", "message": {"content": "One more turn after that."}}) + "\n")
    _run(tmp_path, home, "capture", "--from-file", str(longer), "--session-id", "sess-quokka-0001", "--quiet")
    _build(home, tmp_path, mp, delta=True)
    new_txid = json.loads(old.read_text().split("\ntxid: ", 1)[1].split("\n", 1)[0])
    assert new_txid != old_txid                                       # the newer capture now serves the session
    out = _run(tmp_path, home, "shred", "--txid", old_txid, "--yes", check=False)
    text = out.stdout + out.stderr
    assert out.returncode != 0
    assert f"no plaintext captured-session document existed for {old_txid}" in text, text
    assert f"another served capture of the same session remains: txid {new_txid}" in text, text
    assert old.is_file()                                              # the newer capture's document is not touched


def test_condition5_the_cap_keeps_the_newest_turns(tmp_path, monkeypatch):
    from lbrain.archive import captured
    rows = [{"type": "user", "timestamp": f"2026-11-18T10:{i // 60:02d}:{i % 60:02d}Z",
             "message": {"content": f"turn number {i:04d} " + "pad " * 20}} for i in range(400)]
    payload = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    meta = tmp_path / ("a" * 32 + ".meta.json"); (tmp_path / ("a" * 32 + ".transcript")).write_bytes(payload)
    m = {"sha256": hashlib.sha256(payload).hexdigest(), "_meta_path": str(meta)}
    full = captured.full_text(tmp_path, "", m)
    assert "turn number 0000" in full and "turn number 0399" in full and "older turns omitted" not in full
    monkeypatch.setattr(captured, "FULL_TEXT_CAP", 5000)
    kept = captured.keep_newest(full, captured.FULL_TEXT_CAP)
    assert kept.startswith("… [older turns omitted") and "turn number 0399" in kept and "turn number 0000" not in kept
    body = kept.split("\n\n", 1)[1]
    assert body.startswith("## user ") and len(body) <= 5000      # starts on a turn boundary, within the cap
    # and full_text itself applies it, on the same transcript, through the payload path
    capped = captured.full_text(tmp_path, "", m)
    assert capped == kept


SECRET_SAMPLES = [   # one planted sample per _SECRET_PATTERNS entry, in order; the value after '|' must not survive
    ("postgres://admin:S3cretPassw0rdUrl@db.example.com/x", "S3cretPassw0rdUrl"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ('{"api_key": "jsonSecretValue123"}', "jsonSecretValue123"),
    ('DB_PASSWORD="quoted secret value"', "quoted secret value"),
    ("SECRET_TOKEN=bareSecretValue987", "bareSecretValue987"),
    ("client_secret={unclosedNestedSecret", "unclosedNestedSecret"),
    ("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----"),
    ("use Bearer bearerTokenValue42xyz now", "bearerTokenValue42xyz"),
    ("sk-abcdefgh12345678", "sk-abcdefgh12345678"),
    (PLANTED, PLANTED),
    ("ghp_" + "A1b2C3d4E5" * 4, "ghp_" + "A1b2C3d4E5" * 4),
    ("xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("Zx9" * 16, "Zx9" * 16),
]
PEM_BLOCK = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7MaHk/q1w\n"
             "Hy7/Xq4kq9K2c/Yl0dZ3x+f8bVq/kR9m3T7oPL1ZbkQ/Wqk2Xv9cTt8u=\n-----END RSA PRIVATE KEY-----")


def test_condition6_every_secret_pattern_and_a_pem_block_never_reach_the_document(tmp_path):
    from lbrain.archive import captured
    from lbrain.identity import _SECRET_PATTERNS
    assert len(SECRET_SAMPLES) == len(_SECRET_PATTERNS)
    for (sample, _), (pat, _) in zip(SECRET_SAMPLES, _SECRET_PATTERNS):
        assert pat.search(sample), f"sample does not exercise its pattern: {pat.pattern[:60]}"
    lines = [f"note {i}: {s}" for i, (s, _) in enumerate(SECRET_SAMPLES)] + ["the key follows", PEM_BLOCK, "after the key"]
    rows = [{"type": "user", "timestamp": "2026-11-18T09:00:00Z", "message": {"content": "start of session"}},
            {"type": "user", "timestamp": "2026-11-18T09:00:01Z", "message": {"content": "\n".join(lines)}},
            {"type": "assistant", "timestamp": "2026-11-18T09:00:02Z", "message": {"content": [{"type": "text", "text": "ok"}]}}]
    payload = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    meta = tmp_path / ("b" * 32 + ".meta.json"); (tmp_path / ("b" * 32 + ".transcript")).write_bytes(payload)
    body = captured.full_text(tmp_path, "", {"sha256": hashlib.sha256(payload).hexdigest(), "_meta_path": str(meta)})
    assert "note 0:" in body and "BEGIN RSA PRIVATE KEY" in body          # the plaintext really carried them
    doc = captured.render_doc(session_id="sess-s", captured_at="2026-11-18T09:00:00Z", txid="t" * 43, title="sess-s",
                              namespace="private", source_path=str(tmp_path / "t.jsonl"), text=body)
    for _, secret in SECRET_SAMPLES:
        assert secret not in doc, secret
    for ln in PEM_BLOCK.split("\n"):
        assert ln not in doc, ln
    assert "the key follows" in doc and "after the key" in doc and "note 13:" in doc


CONTRADICTION_LINES = [
    "SUPERSEDED by [[a]] on 2026-11-18",
    "RETIRED — see [[a]]",
    "**Supersedes:** [[a]]",
    "Supersedes: a",
    "status: retired",
    "claims:",
    "  - text: nothing about any marsupial here",
    "    status: retracted",
    "CONTRADICTS [[a]]: 'nothing about any marsupial here' is false",
    "**Last Updated**: 2020-01-01",
]


def test_captured_session_mints_no_claim_contradiction_or_supersession(tmp_path, monkeypatch):
    """CSO 04:02Z question for the first receipt: a captured session must feed NO claim_spans, supersession,
    wikilink or retirement against other records. Planted contradiction-shaped lines (the conversation QUOTING
    every form the index reads as a declaration) mint nothing; the ordinary note a.md stays current and unmarked."""
    mp = monkeypatch
    home = _home(tmp_path, True)
    _build(home, tmp_path, mp, delta=False)
    rows = [{"type": "user", "timestamp": "2026-11-18T09:00:00Z", "message": {"content": "\n".join(CONTRADICTION_LINES)}},
            {"type": "assistant", "timestamp": "2026-11-18T09:00:01Z",
             "message": {"content": [{"type": "text", "text": "noted the wallaby audit"}]}}]
    t = tmp_path / "contra.jsonl"; t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _run(tmp_path, home, "capture", "--from-file", str(t), "--session-id", "sess-contra", "--quiet")
    rep = _build(home, tmp_path, mp, delta=True)
    assert rep["captured_docs"]["post_sweep"]["written"] == 1, rep["captured_docs"]
    doc = home / "captures" / "captured-session-sess-contra.md"
    db = home / "epochs" / (home / "epochs" / "CURRENT").read_text().strip() / "brain.db"
    con = eb._connect_vec(db)
    rel = con.execute("SELECT rel_path FROM docs WHERE abs_path = ?", (str(doc),)).fetchone()[0]
    for table in ("claim_spans", "supersessions", "wikilinks"):
        assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE src_path = ?", (rel,)).fetchone()[0] == 0, table
    assert con.execute("SELECT COUNT(*) FROM supersessions").fetchone()[0] == 0     # nothing anywhere names a.md
    assert con.execute("SELECT COUNT(*) FROM claim_spans").fetchone()[0] == 0
    con.close()
    def headers(out):   # the record header lines: where SUPERSEDED / RETIRED markers are rendered
        return [ln for ln in out.splitlines() if ln.strip().startswith("src:")]
    s = _run(tmp_path, home, "search", "--current-only", "marsupial").stdout
    assert "src: a.md" in s and not any(("SUPERSEDED" in h or "RETIRED" in h) for h in headers(s)), s
    c = _run(tmp_path, home, "search", "--current-only", "wallaby audit").stdout
    assert "captured session sess-contra" in c and not any("RETIRED" in h for h in headers(c)), c   # not self-retired
    # the exemption holds at parse time even for frontmatter the engine did not write
    from lbrain.index import parse
    p = tmp_path / "hand.md"
    p.write_text('---\nkind: "captured-session"\ntype: "captured-session"\nsupersedes: a\nclaims:\n'
                 '  - text: nothing about any marsupial here\n    status: retracted\n---\n# x\n\n**Supersedes:** [[a]]\nsee [[a]]\n')
    d = parse(p)
    assert d.claims == [] and d.supersedes == [] and d.wikilinks == []
