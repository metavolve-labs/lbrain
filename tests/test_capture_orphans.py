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


def test_orphan_capture_is_titled_by_session_id_like_the_sessionend_hook(tmp_path):
    """Archives group a session's snapshots by title (the SessionEnd hook titles a capture with its session id).
    A recovered capture must carry the same title, or its snapshots never group with the session's others."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    titles = [json.loads(m.read_text())["title"] for m in staging_dir(home).glob(f"*{META_SUFFIX}")]
    assert titles == [SID_A]


# ---- CSO round 2: the reader itself, against a fabricated process table -----------------------------------------

def _fake_proc(root: Path, pid: int, argv: list[str], start_ticks: int = 100, environ: dict | None = None,
               environ_unreadable: bool = False, cwd: Path | None = None, cmdline_unreadable: bool = False):
    d = root / str(pid); d.mkdir(parents=True)
    if cmdline_unreadable:
        (d / "cmdline").mkdir()                                     # reading a directory raises: unreadable
    else:
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 10   # field 22 (index 19 after ')') = starttime
    (d / "stat").write_text(f"{pid} (proc) " + " ".join(fields), encoding="ascii")
    if environ_unreadable:
        (d / "environ").mkdir()
    else:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (environ or {}).items()) + b"\0")
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)
    return d


def _fake_root(tmp_path):
    root = tmp_path / "proc"; root.mkdir()
    (root / "stat").write_text("cpu 0\nbtime 1000\n", encoding="ascii")
    return root


def test_reader_unreadable_environ_gives_unknown_location_and_recover_reports_it(tmp_path):
    root = _fake_root(tmp_path)
    _fake_proc(root, 4242, ["claude"], environ_unreadable=True)
    live = orphans._live_processes(root)
    assert [(p.kind, p.location) for p in live] == [("claude", None)]
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=live)
    assert _staged_sids(home) == [] and rep.errors


def test_reader_unreadable_cmdline_raises(tmp_path):
    root = _fake_root(tmp_path)
    _fake_proc(root, 4243, ["claude"], cmdline_unreadable=True)
    with pytest.raises(orphans.LivenessUnreadable):
        orphans._live_processes(root)


def test_reader_start_is_clamped_to_the_proc_dir_mtime_under_a_stale_btime(tmp_path):
    """CSO round 2 (F3 had no flipping test): a btime 2 h late must not move the start past /proc/<pid>'s mtime."""
    root = _fake_root(tmp_path)
    d = _fake_proc(root, 4244, ["claude"], start_ticks=100, environ={"HOME": str(tmp_path)})
    os.utime(d, (5000, 5000))
    live = orphans._live_processes(root, btime=5000 + 7200)        # computed start ~ 12201, i.e. 2 h after the mtime
    assert live[0].start == pytest.approx(5000)


def test_reader_falls_back_to_the_PROCESS_home_not_the_scanners(tmp_path):
    """CSO round 2 #1: no GROK_HOME in the process env -> the process's own HOME/.grok, never the scanner's."""
    root = _fake_root(tmp_path)
    proc_home = tmp_path / "someone-else"; proc_home.mkdir()
    _fake_proc(root, 4245, ["grok"], environ={"HOME": str(proc_home)})
    live = orphans._live_processes(root)
    assert [(p.kind, p.location) for p in live] == [("grok", (proc_home / ".grok").resolve())]


def test_reader_no_key_and_no_home_is_unknown(tmp_path):
    root = _fake_root(tmp_path)
    _fake_proc(root, 4246, ["codex"], environ={"PATH": "/usr/bin"})
    assert [(p.kind, p.location) for p in orphans._live_processes(root)] == [("codex", None)]


def test_reader_resolves_a_relative_config_dir_against_the_process_cwd(tmp_path):
    root = _fake_root(tmp_path)
    pcwd = tmp_path / "proc-cwd"; pcwd.mkdir()
    _fake_proc(root, 4247, ["claude"], environ={"CLAUDE_CONFIG_DIR": "rel/.claude-x"}, cwd=pcwd)
    assert orphans._live_processes(root)[0].location == (pcwd / "rel" / ".claude-x").resolve()


def test_reader_finds_cli_js_after_many_node_flags(tmp_path):
    """CSO round 2 #2: the claude-code cli.js may follow any number of node flags."""
    root = _fake_root(tmp_path)
    argv = ["node"] + [f"--flag{i}" for i in range(9)] + ["/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]
    _fake_proc(root, 4248, argv, environ={"HOME": str(tmp_path)})
    assert [p.kind for p in orphans._live_processes(root)] == ["claude"]


def test_live_grok_protects_its_sessions(tmp_path):
    """CSO round 2: the grok kind had no liveness test."""
    home, cfg = _home(tmp_path)
    g = tmp_path / ".grok"
    s = g / "sessions" / "gsess-1" / "chat_history.jsonl"; s.parent.mkdir(parents=True)
    s.write_text(json.dumps({"timestamp": "2026-10-17T01:00:00Z"}) + "\n")
    rep = orphans.recover(cfg, home, grok_homes=[g], live=[orphans.LiveProc("grok", g.resolve(), 0.0, None)])
    assert _staged_sids(home) == [] and rep.live == ["gsess-1"]


def test_older_form_when_the_captured_payload_was_drained(tmp_path):
    """§4.2.5, the drained branch: the payload was reclaimed after its sweep; a form no larger is older."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    for pl in staging_dir(home).glob(f"*{PAYLOAD_SUFFIX}"):
        pl.unlink()                                                 # drained, as after a verified sweep
    p.write_text(json.dumps({"timestamp": "2026-10-16T21:00:00Z"}) + "\n")   # different bytes, not larger
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [] and rep.older == 1


def test_cli_fail_closed_exits_non_zero(tmp_path, monkeypatch):
    """CSO round 2 #3 / round 3: the fail-closed path reports AND exits non-zero. In-process (CliRunner) with the
    reader monkeypatched: there is deliberately no environment lever to point the CLI at a fake process table."""
    from click.testing import CliRunner
    import lbrain.cli as lcli
    import lbrain.config as lconfig
    home, _ = _home(tmp_path)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nembedding_provider = "local"\n')
    monkeypatch.setattr(lcli, "CONFIG_DIR", home)
    monkeypatch.setattr(lconfig, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setenv("LBRAIN_ARCHIVE_PASSPHRASE", "x" * 32)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    monkeypatch.setattr(orphans, "_live_processes", lambda *a, **k: [orphans.LiveProc("claude", None, 0.0, None)])
    res = CliRunner().invoke(lcli.main, ["capture-orphans", "--claude-config-dir", str(c)])
    assert res.exit_code != 0 and not _staged_sids(home), (res.exit_code, res.output)


def test_no_environment_variable_redirects_the_process_table(tmp_path, monkeypatch):
    """CSO round 3 D1: $LBRAIN_PROC_ROOT (or any env) must NOT redirect the reader. With it pointing at an empty
    table, a REAL live claude stand-in must still be seen."""
    empty = _fake_root(tmp_path)
    monkeypatch.setenv("LBRAIN_PROC_ROOT", str(empty))
    cfg_dir = tmp_path / ".claude-envlever"; cfg_dir.mkdir()
    proc = _stand_in("claude", ["--resume", SID_B], {"CLAUDE_CONFIG_DIR": str(cfg_dir)})
    try:
        assert _wait_live(lambda p: p.session_id == SID_B), "the env var redirected the process table"
    finally:
        proc.kill(); proc.wait()


def test_reader_unreadable_stat_raises(tmp_path):
    """CSO round 3 F1c: an unreadable /proc/<pid>/stat is not skipped."""
    root = _fake_root(tmp_path)
    d = _fake_proc(root, 4250, ["claude"], environ={"HOME": str(tmp_path)})
    (d / "stat").unlink(); (d / "stat").mkdir()
    with pytest.raises(orphans.LivenessUnreadable):
        orphans._live_processes(root)


def test_symlink_pointing_inside_the_location_is_not_followed(tmp_path):
    """CSO round 3 F6b: a symlink whose target is INSIDE the location is still a symlink, never captured twice."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    target = c / "notes" / "scratch.txt"; target.parent.mkdir(parents=True)        # INSIDE the location, not a transcript
    target.write_text(json.dumps({"timestamp": "2026-10-16T22:00:00Z", "x": "distinct content"}) + "\n")
    link = c / "projects" / "-proj" / f"{SID_B}.jsonl"
    link.symlink_to(target)
    orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert _staged_sids(home) == [SID_A]


def test_reader_unreadable_cwd_is_unknown_not_dropped(tmp_path):
    """CSO round 3 N1: a relative CLAUDE_CONFIG_DIR with no readable cwd -> unknown location (fail closed), never
    a process dropped from the live list."""
    root = _fake_root(tmp_path)
    _fake_proc(root, 4251, ["claude"], environ={"CLAUDE_CONFIG_DIR": "rel/.claude-y"})   # no cwd link
    assert [(p.kind, p.location) for p in orphans._live_processes(root)] == [("claude", None)]


def test_reader_resolves_a_relative_home_against_the_process_cwd(tmp_path):
    root = _fake_root(tmp_path)
    pcwd = tmp_path / "proc-cwd2"; pcwd.mkdir()
    _fake_proc(root, 4252, ["codex"], environ={"HOME": "relhome"}, cwd=pcwd)
    assert orphans._live_processes(root)[0].location == (pcwd / "relhome" / ".codex").resolve()


def test_unknown_location_of_an_unscanned_kind_is_not_an_error(tmp_path):
    """CSO round 3 noise: an unknown-location claude process does not fail a codex-only scan."""
    home, cfg = _home(tmp_path)
    cx = tmp_path / ".codex"; (cx / "sessions").mkdir(parents=True)
    rep = orphans.recover(cfg, home, codex_homes=[cx], live=[orphans.LiveProc("claude", None, 0.0, None)])
    assert rep.errors == []


# --- --since cutoff (first wiring must not sweep a seat's history) and streaming hashing ---------------------------

def test_since_skips_transcripts_last_written_before_the_cutoff(tmp_path):
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    old = _claude_transcript(c, SID_A, "2026-10-01T00:00:00Z")
    new = _claude_transcript(c, SID_B, "2026-10-16T21:00:00Z")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (3_000_000, 3_000_000))
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[], since=2_000_000)
    assert rep.staged == [SID_B] and rep.before_cutoff == 1 and _staged_sids(home) == [SID_B]


def test_since_boundary_is_inclusive_and_a_post_cutoff_write_is_captured(tmp_path):
    """A session killed after wiring always has a write after the cutoff: it is captured, even if it began before."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    p = _claude_transcript(c, SID_A, "2026-10-01T00:00:00Z")
    os.utime(p, (2_000_000, 2_000_000))                                           # exactly at the cutoff
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[], since=2_000_000)
    assert rep.staged == [SID_A] and rep.before_cutoff == 0


def test_without_since_history_is_still_captured(tmp_path):
    """Backfill stays available: no cutoff means every orphan, however old."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    os.utime(_claude_transcript(c, SID_A, "2026-01-01T00:00:00Z"), (1000, 1000))
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[])
    assert rep.staged == [SID_A] and rep.before_cutoff == 0


def test_streaming_hash_matches_the_whole_file_hash_across_blocks(tmp_path):
    import hashlib
    p = tmp_path / "big.jsonl"
    data = os.urandom((1 << 20) * 2 + 12345)                                     # spans three 1 MiB blocks
    p.write_bytes(data)
    assert orphans._sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_dry_run_and_known_content_never_load_a_whole_transcript(tmp_path, monkeypatch):
    """Peak memory is one transcript at staging, not all of them: hashing streams, dry run reads no payload."""
    home, cfg = _home(tmp_path)
    c = tmp_path / ".claude-seat"
    _claude_transcript(c, SID_A, "2026-10-16T21:00:00Z")
    orphans.recover(cfg, home, claude_dirs=[c], live=[])                          # captured once
    _claude_transcript(c, SID_B, "2026-10-16T22:00:00Z")
    real = Path.read_bytes

    def guarded(self):
        if self.suffix == ".jsonl" and c in self.parents:
            raise AssertionError(f"whole transcript loaded: {self}")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    rep = orphans.recover(cfg, home, claude_dirs=[c], live=[], dry_run=True)
    assert rep.staged == [SID_B] and rep.known == 1


def test_cli_since_accepts_utc_minute_and_epoch_and_refuses_garbage(tmp_path):
    home, _ = _home(tmp_path)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nembedding_provider = "local"\n')
    c = tmp_path / ".claude-seat"
    import datetime as dt
    cut = dt.datetime(2026, 10, 17, 5, 0, tzinfo=dt.timezone.utc).timestamp()
    os.utime(_claude_transcript(c, SID_A, "2026-10-01T00:00:00Z"), (cut - 60, cut - 60))   # a minute before
    os.utime(_claude_transcript(c, SID_B, "2026-10-17T05:00:30Z"), (cut + 60, cut + 60))   # a minute after
    for since in ("2026-10-17T05:00Z", str(int(cut))):                     # the minute is read as UTC, not local
        r = _cli(home, "--claude-config-dir", str(c), "--dry-run", "--since", since)
        assert r.returncode == 0, (since, r.stdout, r.stderr)
        assert "before cutoff 1" in r.stdout and "would stage 1" in r.stdout.lower(), (since, r.stdout)
    bad = _cli(home, "--claude-config-dir", str(c), "--dry-run", "--since", "yesterday")
    assert bad.returncode == 2 and "--since" in (bad.stdout + bad.stderr), (bad.stdout, bad.stderr)
