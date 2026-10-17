"""`lbrain whoami` must report the same counts as `lbrain stats`, or say why it cannot.

Observed on the CCO home (2026-10-17): `whoami` printed
``indexed: None docs · None chunks · None embedded`` while `lbrain stats` reported
1543 / 9265 / 9265 for the same epoch. `whoami` wrapped open_store + stats() in a bare
``except Exception: pass`` and then formatted the missing keys as Python ``None``. The
failure that produced the gap was swallowed, so the trust command printed a value that
looks like data and names no cause.

Contract pinned here:
  * parity: on an epoch-managed home, whoami counts == stats counts (same epoch db);
  * when counts cannot be read, whoami prints ``unknown (<reason>)`` and never ``None``;
  * a count failure never changes the identity section (no weakening of identity checks).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from lbrain import epoch, epoch_build
from lbrain.identity import describe

from _coldembed import seed_brain

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _mk_epoch_home(tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.md").write_text("# Alpha\n\nThe quicksilver archive holds record one.\n")
    (src / "beta.md").write_text("# Beta\n\nAnother distinctive passage, number two.\n")
    (src / "gamma.md").write_text("# Gamma\n\nA third record about lanterns.\n")
    (home / "config.toml").write_text(
        'embedding_provider = "local"\n'
        f'embedding_model = "{MODEL}"\n'
        f"embedding_dim = {DIM}\n"
        f'db_path = "{home / "brain.db"}"\n'
        f'sources = ["{src}"]\n')
    (home / "identity.json").write_text('{"who": "test-seat"}\n')
    seed_brain(home, DIM)
    from types import SimpleNamespace
    report = epoch_build.build(home, SimpleNamespace(sources=[str(src)], embedding_dim=DIM,
                                                     db_path=home / "brain.db"))
    assert report["published"], report
    assert epoch.current_epoch_id(home) == report["epoch_id"]
    return home


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, LBRAIN_HOME=str(home))
    return subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)


def _stats_counts(home: Path) -> tuple[int, int, int]:
    p = _run(home, "stats")
    assert p.returncode == 0, p.stdout + p.stderr
    got = {k: int(v) for k, v in re.findall(r"^(docs|chunks|embedded):\s+(\d+)$", p.stdout, re.M)}
    return got["docs"], got["chunks"], got["embedded"]


def _whoami_line(home: Path) -> str:
    p = _run(home, "whoami")
    assert p.returncode == 0, p.stdout + p.stderr
    lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("indexed:")]
    assert len(lines) == 1, p.stdout
    return lines[0]


def _identity_block(stdout: str) -> str:
    """Everything whoami prints before the brain section: the identity claims."""
    return stdout.split("\n  brain\n", 1)[0]


# ---------- normal path (control) ----------

def test_whoami_counts_equal_stats_counts_on_the_same_epoch(tmp_path):
    home = _mk_epoch_home(tmp_path)
    d, c, e = _stats_counts(home)
    assert d == 3 and c >= 3 and e == c, (d, c, e)
    line = _whoami_line(home)
    m = re.search(r"indexed:\s+(\d+) docs · (\d+) chunks · (\d+) embedded$", line)
    assert m, line
    assert tuple(int(x) for x in m.groups()) == (d, c, e)
    assert "None" not in line and "unknown" not in line


def test_whoami_json_counts_equal_stats_and_carry_no_unknown_reason(tmp_path):
    home = _mk_epoch_home(tmp_path)
    p = _run(home, "whoami", "--json")
    assert p.returncode == 0, p.stderr
    b = json.loads(p.stdout)["brain"]
    assert (b["docs"], b["chunks"], b["embedded"]) == _stats_counts(home)
    assert b["counts_unknown"] == ""


# ---------- the defect: a count failure must name itself ----------

def test_dangling_current_prints_unknown_with_reason_never_none(tmp_path):
    home = _mk_epoch_home(tmp_path)
    (home / "epochs" / "CURRENT").write_text("20000101T000000Z-1-0001\n")
    p = _run(home, "whoami")
    assert p.returncode == 0, p.stdout + p.stderr
    line = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("indexed:")][0]
    assert "None" not in line, line
    assert "unknown (" in line, line
    assert "does not exist" in line, line  # the real cause, not a generic word


def test_corrupt_epoch_db_prints_unknown_with_reason_never_none(tmp_path):
    home = _mk_epoch_home(tmp_path)
    db = epoch.epoch_db(home, epoch.current_epoch_id(home))
    db.write_bytes(b"this is not a sqlite database" * 100)
    p = _run(home, "whoami")
    assert p.returncode == 0, p.stdout + p.stderr
    line = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("indexed:")][0]
    assert "None" not in line, line
    assert re.search(r"unknown \(.*\S.*\)", line), line


def test_json_surface_names_the_reason_when_counts_are_unknown(tmp_path):
    home = _mk_epoch_home(tmp_path)
    (home / "epochs" / "CURRENT").write_text("20000101T000000Z-1-0001\n")
    p = _run(home, "whoami", "--json")
    assert p.returncode == 0, p.stderr
    b = json.loads(p.stdout)["brain"]
    assert b["docs"] is None and b["chunks"] is None and b["embedded"] is None
    assert "does not exist" in b["counts_unknown"], b


def test_a_count_failure_never_changes_the_identity_section(tmp_path):
    home = _mk_epoch_home(tmp_path)
    good = _run(home, "whoami")
    gj = json.loads(_run(home, "whoami", "--json").stdout)
    (home / "epochs" / "CURRENT").write_text("20000101T000000Z-1-0001\n")
    bad = _run(home, "whoami")
    bj = json.loads(_run(home, "whoami", "--json").stdout)
    assert _identity_block(good.stdout) == _identity_block(bad.stdout)
    assert gj["identity"] == bj["identity"]


# ---------- unit: describe() never emits a bare None without a reason ----------

def test_describe_with_partial_stats_names_the_missing_key(tmp_path, monkeypatch):
    from lbrain.config import Config
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    b = describe(Config.load(), {"docs": 4})["brain"]
    assert b["docs"] == 4 and b["chunks"] is None
    assert "chunks" in b["counts_unknown"] and "embedded" in b["counts_unknown"]


def test_describe_carries_the_supplied_reason(tmp_path, monkeypatch):
    from lbrain.config import Config
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    b = describe(Config.load(), {}, counts_error="store did not open: boom")["brain"]
    assert b["counts_unknown"] == "store did not open: boom"


def test_describe_with_full_stats_has_no_reason(tmp_path, monkeypatch):
    from lbrain.config import Config
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    b = describe(Config.load(), {"docs": 1, "chunks": 2, "embedded": 2})["brain"]
    assert b["counts_unknown"] == ""


def test_mcp_whoami_names_the_reason_too(tmp_path, monkeypatch):
    """The MCP tool shares describe() and had the same bare except; it must not diverge."""
    home = _mk_epoch_home(tmp_path)
    (home / "epochs" / "CURRENT").write_text("20000101T000000Z-1-0001\n")
    import lbrain.cli as cli
    import lbrain.config as config
    import lbrain.epoch as ep
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    for mod in (config, cli, ep):
        for attr, val in (("CONFIG_DIR", home), ("CONFIG_PATH", home / "config.toml")):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val, raising=False)
    from lbrain import mcp_server
    if hasattr(mcp_server, "CONFIG_DIR"):
        monkeypatch.setattr(mcp_server, "CONFIG_DIR", home, raising=False)
    out = mcp_server.lair_whoami()
    payload = json.loads(out[out.index("{"):])
    assert "does not exist" in payload["brain"]["counts_unknown"], payload["brain"]


def test_read_counts_names_a_failing_count_query_and_still_closes(monkeypatch):
    """A store that OPENS but whose count query raises: the reason must be the query's own
    error (not a generic 'no docs'), and the store must still be closed (lease released)."""
    from lbrain import epoch as ep
    from lbrain.identity import read_counts

    closed = []

    class _Store:
        def stats(self):
            raise RuntimeError("no such module: vec0")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ep, "open_store", lambda cfg: _Store())
    stats, reason = read_counts(object())
    assert stats == {}
    assert reason.startswith("count query failed") and "no such module: vec0" in reason, reason
    assert closed == [True]


def test_read_counts_returns_real_counts_with_no_reason(monkeypatch):
    from lbrain import epoch as ep
    from lbrain.identity import read_counts

    class _Store:
        def stats(self):
            return {"docs": 7, "chunks": 9, "embedded": 9}

        def close(self):
            pass

    monkeypatch.setattr(ep, "open_store", lambda cfg: _Store())
    assert read_counts(object()) == ({"docs": 7, "chunks": 9, "embedded": 9}, "")
