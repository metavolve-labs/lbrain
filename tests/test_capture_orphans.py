"""Kill survival (b): wake-time orphan capture. The acceptance test is CSO plan v0.1 §4 (2026-10-16).

Each test names its §4 item. Behaviour only: what was staged (session ids, spool metas), never message wording.
Liveness is injected (`live=`) so the tests do not depend on what runs on the test host; one test exercises the
real process-table reader against this test's own process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import orphans
from lbrain.spool import META_SUFFIX, PAYLOAD_SUFFIX, staging_dir

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SID_C = "cccccccc-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _no_seat_claim(monkeypatch):
    monkeypatch.delenv("LBRAIN_SEAT", raising=False)


def _home(tmp_path):
    h = tmp_path / "home"; h.mkdir()
    return h, SimpleNamespace(db_path=h / "brain.db")


def _claude_transcript(cfg_dir: Path, sid: str, ts: str, project="-proj", body="hello"):
    p = cfg_dir / "projects" / project / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"type": "user", "timestamp": ts, "message": body}) + "\n", encoding="utf-8")
    return p


def _staged_sids(home: Path) -> list[str]:
    d = staging_dir(home)
    return sorted(json.loads(m.read_text())["session_id"] for m in d.glob(f"*{META_SUFFIX}")) if d.is_dir() else []


def test_4_1_killed_session_is_staged(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [SID_A] and _staged_sids(home) == [SID_A]


def test_4_2_negative_controls(tmp_path):
    home, cfg = _home(tmp_path)
    mine = tmp_path / ".claude-seat"
    other = tmp_path / ".claude-other-seat"
    _claude_transcript(mine, SID_A, "2026-10-16T21:00:00Z")
    _claude_transcript(other, SID_B, "2026-10-16T21:00:00Z")                      # another seat's
    (tmp_path / "loose").mkdir(); (tmp_path / "loose" / f"{SID_C}.jsonl").write_text("{}\n")  # outside every dir
    np = mine / "projects" / "-proj" / "notes.jsonl"; np.write_text("{}\n")      # not transcript-shaped
    rep = orphans.recover(cfg, home, claude_dirs=[mine], live=[])
    assert _staged_sids(home) == [SID_A]


def test_4_2_live_session_by_resume_id_is_not_captured(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    _claude_transcript(c, SID_B, "2026-10-16T21:01:00Z")
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[orphans.LiveProc("claude", tmp_path / "elsewhere", 0.0, SID_B)])
    assert _staged_sids(home) == [SID_A] and rep.live == [SID_B]


def test_4_2_live_process_on_the_same_config_dir_protects_newer_transcripts(tmp_path):
    """A live claude on this config dir started at T: transcripts modified at or after T may be its own."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    old = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    new = _claude_transcript(c, SID_B, "2026-10-16T21:01:00Z")
    os.utime(old, (1000, 1000)); os.utime(new, (3000, 3000))
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[orphans.LiveProc("claude", c.resolve(), 2000.0, None)])
    assert _staged_sids(home) == [SID_A] and rep.live == [SID_B]


def test_4_3_exactly_once(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    rep2 = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep2.staged == [] and rep2.known == 1 and len(list(staging_dir(home).glob(f"*{META_SUFFIX}"))) == 1


def test_4_3_mtime_change_alone_is_not_new_content(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    os.utime(p, (5000, 5000))
    assert orphans.recover(cfg, home, claude_dirs=[c], live=[]).staged == []


def test_4_4_grown_after_capture_is_captured_again(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-16T21:05:00Z"}) + "\n")
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [SID_A] and _staged_sids(home) == [SID_A, SID_A]   # two contents, one session, none lost


def test_4_5_order_is_transcript_time_not_mtime(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    late = _claude_transcript(c, SID_A, "2026-10-16T23:00:00Z")
    early = _claude_transcript(c, SID_B, "2026-10-16T01:00:00Z")
    os.utime(late, (1000, 1000)); os.utime(early, (9000, 9000))           # mtimes say the opposite
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [SID_B, SID_A]


def test_4_6_torn_spool_entry_is_completed(tmp_path):
    """A payload renamed without its meta (a kill between the two renames) is completed by the next run."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    import hashlib
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    d = staging_dir(home); d.mkdir(parents=True)
    (d / f"{digest[:32]}{PAYLOAD_SUFFIX}").write_bytes(p.read_bytes())        # torn: no meta
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert (d / f"{digest[:32]}{META_SUFFIX}").is_file() and _staged_sids(home) == [SID_A]


def test_4_8_codex_parity(tmp_path):
    home, cfg = _home(tmp_path)
    cx = tmp_path / ".codex"
    r = cx / "sessions" / "2026" / "10" / "16" / f"rollout-2026-10-16T21-00-00-{SID_C}.jsonl"
    r.parent.mkdir(parents=True)
    r.write_text(json.dumps({"timestamp": "2026-10-16T21:00:00Z", "type": "session_meta"}) + "\n")
    (r.parent / "history.jsonl").write_text("{}\n")                          # not a rollout
    rep = orphans.recover(cfg, home, codex_homes=[cx], live=[])
    assert _staged_sids(home) == [SID_C]


def test_4_9_every_declared_location_is_scanned(tmp_path):
    home, cfg = _home(tmp_path)
    a, b = tmp_path / ".claude-seat", tmp_path / ".claude-test"
    _claude_transcript(a, SID_A, "2026-10-16T21:00:00Z")
    _claude_transcript(b, SID_B, "2026-10-16T21:01:00Z")
    orphans.recover(cfg, home, claude_dirs=[a, b], live=[])
    assert _staged_sids(home) == [SID_A, SID_B]


def _stand_in(argv0, args, env_extra):
    """A real process whose argv is [argv0, *args] (python sleeping underneath), with extra env."""
    env = dict(os.environ, **env_extra)
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.Popen(["bash", "-c", f'exec -a {argv0} {sys.executable} -c "import time; time.sleep(30)" {quoted}'], env=env)


def _wait_live(pred):
    for _ in range(50):
        live = orphans._live_processes()
        hits = [p for p in live if pred(p)]
        if hits:
            return hits
        time.sleep(0.1)
    return []


def test_live_reader_finds_a_real_claude_process_with_its_config_dir_and_resume_id(tmp_path):
    cfg_dir = tmp_path / ".claude-live"; cfg_dir.mkdir()
    proc = _stand_in("claude", ["--resume", SID_C], {"CLAUDE_CONFIG_DIR": str(cfg_dir)})
    try:
        hits = _wait_live(lambda p: p.session_id == SID_C)
        assert hits and hits[0].kind == "claude" and hits[0].location == cfg_dir.resolve(), hits
        assert hits[0].start <= os.stat(f"/proc/{proc.pid}").st_mtime + 1e-6   # clamped to the /proc mtime (CSO #3)
    finally:
        proc.kill(); proc.wait()


def test_node_launched_claude_and_resume_equals_are_matched(tmp_path):
    """CSO #4: a node-launched cli.js is a claude process; `--resume=<sid>` names its session."""
    cfg_dir = tmp_path / ".claude-node"; cfg_dir.mkdir()
    proc = _stand_in("node", ["/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", f"--resume={SID_A}"],
                     {"CLAUDE_CONFIG_DIR": str(cfg_dir)})
    try:
        hits = _wait_live(lambda p: p.session_id == SID_A)
        assert hits and hits[0].kind == "claude" and hits[0].location == cfg_dir.resolve(), hits
    finally:
        proc.kill(); proc.wait()


def test_live_codex_protects_its_rollouts(tmp_path):
    """CSO #2: a live codex on CODEX_HOME protects rollouts modified after it started (Codex liveness parity)."""
    home, cfg = _home(tmp_path)
    cx = tmp_path / ".codex"
    r = cx / "sessions" / "2026" / "10" / "17" / f"rollout-2026-10-17T01-00-00-{SID_C}.jsonl"
    r.parent.mkdir(parents=True)
    proc = _stand_in("codex", [], {"CODEX_HOME": str(cx)})
    try:
        assert _wait_live(lambda p: p.kind == "codex" and p.location == cx.resolve())
        time.sleep(0.05)
        r.write_text(json.dumps({"timestamp": "2026-10-17T01:00:00Z"}) + "\n")   # written while codex lives
        rep = orphans.recover(cfg, home, codex_homes=[cx])                          # REAL process table
        assert _staged_sids(home) == [] and SID_C in rep.live
    finally:
        proc.kill(); proc.wait()


def test_unreadable_process_table_fails_closed(tmp_path, monkeypatch):
    """CSO #1: liveness unknown -> NOTHING staged, an error reported (rc != 0 at the CLI)."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    def boom():
        raise orphans.LivenessUnreadable("/proc unreadable: test")
    monkeypatch.setattr(orphans, "_live_processes", boom)
    rep = orphans.recover(cfg, home, claude_dirs=[c])
    assert _staged_sids(home) == [] and rep.errors


def test_unreadable_location_of_a_live_process_protects_its_whole_kind(tmp_path):
    """CSO #1 (per process): a live claude whose environment could not be read protects every claude location."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[orphans.LiveProc("claude", None, 9e9, None)])
    assert _staged_sids(home) == [] and rep.live == [SID_A]


def test_4_2_5_an_older_form_of_a_captured_session_is_not_restaged(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    short = p.read_bytes()
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-16T21:05:00Z"}) + "\n")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])                         # captures the LONG form
    p.write_bytes(short)                                                          # an older form reappears
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [] and rep.older == 1


def test_symlinked_transcript_is_not_followed(tmp_path):
    """CSO #6: a transcript linked in from outside the declared location is not captured."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    outside = tmp_path / "outside" / f"{SID_B}.jsonl"; outside.parent.mkdir()
    outside.write_text(json.dumps({"timestamp": "2026-10-16T21:00:00Z"}) + "\n")
    link = c / "projects" / "-proj" / f"{SID_B}.jsonl"; link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert _staged_sids(home) == []


def test_grown_content_with_an_old_mtime_is_still_captured(tmp_path):
    """CSO B2 note: identity is content, never mtime. Content grew, mtime set back below the capture: staged."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    os.utime(p, (5000, 5000))
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-16T21:05:00Z"}) + "\n")
    os.utime(p, (1000, 1000))                                                     # mtime now OLDER than before
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [SID_A]


def _cli(home, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("LBRAIN_ARCHIVE_PASSPHRASE", "LBRAIN_SEAT")}
    env["LBRAIN_HOME"] = str(home)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "lbrain.cli", "capture-orphans", *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_4_7_staged_without_passphrase_is_loud(tmp_path):
    home, _ = _home(tmp_path)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nembedding_provider = "local"\n')
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    r = _cli(home, "--claude-config-dir", str(c))
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert "1 capture(s) staged" in (r.stdout + r.stderr), (r.stdout, r.stderr)        # the N-staged line (CSO)
    r2 = _cli(home, "--claude-config-dir", str(c), env_extra={"LBRAIN_ARCHIVE_PASSPHRASE": "x" * 32})
    assert r2.returncode == 0, (r2.stdout, r2.stderr)


def test_no_declared_location_refuses(tmp_path):
    home, _ = _home(tmp_path)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nembedding_provider = "local"\n')
    assert _cli(home).returncode == 2
