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
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[(tmp_path / "elsewhere", 0.0, SID_B)])
    assert _staged_sids(home) == [SID_A] and rep.live == [SID_B]


def test_4_2_live_process_on_the_same_config_dir_protects_newer_transcripts(tmp_path):
    """A live claude on this config dir started at T: transcripts modified at or after T may be its own."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    old = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    new = _claude_transcript(c, SID_B, "2026-10-16T21:01:00Z")
    os.utime(old, (1000, 1000)); os.utime(new, (3000, 3000))
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[(c.resolve(), 2000.0, None)])
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


def test_live_reader_finds_a_real_claude_process_with_its_config_dir_and_resume_id(tmp_path):
    """The REAL process-table reader (no injection): a stand-in process whose argv[0] is `claude`, launched with
    CLAUDE_CONFIG_DIR and `--resume <sid>`, must be read back with that config dir and that session id."""
    cfg_dir = tmp_path / ".claude-live"; cfg_dir.mkdir()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg_dir))
    proc = subprocess.Popen(["bash", "-c", f'exec -a claude {sys.executable} -c "import time; time.sleep(30)" --resume {SID_C}'],
                            env=env)
    try:
        for _ in range(50):
            live = orphans._live_claude_processes()
            if any(sid == SID_C for _, _, sid in live):
                break
            time.sleep(0.1)
        hits = [(cfg, start, sid) for cfg, start, sid in live if sid == SID_C]
        assert hits and hits[0][0] == cfg_dir.resolve(), live
        assert abs(hits[0][1] - time.time()) < 60                       # start time read correctly
    finally:
        proc.kill(); proc.wait()


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
    r2 = _cli(home, "--claude-config-dir", str(c), env_extra={"LBRAIN_ARCHIVE_PASSPHRASE": "x" * 32})
    assert r2.returncode == 0, (r2.stdout, r2.stderr)


def test_no_declared_location_refuses(tmp_path):
    home, _ = _home(tmp_path)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nembedding_provider = "local"\n')
    assert _cli(home).returncode == 2
