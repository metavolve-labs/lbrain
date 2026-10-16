"""Wake-time orphan capture: kill survival, layer (b) (CSO plan v0.1, 2026-10-16).

The SessionEnd hook stages a transcript into the capture spool only when the session ENDS. A hard kill
(SIGKILL, power loss, an OOM) never runs it, so the transcript stays on disk (layer L1) but never reaches the
brain. This module runs from a seat's launcher BEFORE the new session starts: it finds the seat's transcripts
that the spool has never seen in their current form and stages them through the normal gated spool
(``spool_capture``: W1/W2 run before any byte lands). The next ``epoch build`` sweeps them into the brain.

What it must never do (CSO §4.2 negative controls):
- capture another seat's transcript: it scans ONLY the locations the launcher declares for this seat;
- capture a file that is not a transcript: Claude Code `<cfg>/projects/<project>/<uuid>.jsonl`, Codex
  `<codex>/sessions/**/rollout-*.jsonl`, Grok `<grok>/sessions/<id>/chat_history.jsonl`, nothing else;
- capture a LIVE session: Claude Code does not keep its transcript open (measured 2026-10-16: no live claude
  pid holds a .jsonl fd), so liveness is read from the process table. (1) A live `claude --resume <sid>` names
  its session exactly. (2) Conservatively, a transcript in a config dir that a live claude process uses, last
  modified after that process started, may be that process's: it is skipped now and taken by SessionEnd or the
  next wake;
- capture the same content twice: identity is the FULL-content sha256 against every spool meta (staged and
  swept), never mtime. A transcript that grew after its capture has a new hash and is staged again whole, and its
  meta records the session id, so the newest capture of a session supersedes the older by content.
Order (§4.5): staged in the order of each transcript's first timestamp, never mtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .spool import META_SUFFIX, staging_dir

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CODEX_RE = re.compile(r"^rollout-.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


@dataclass
class Transcript:
    path: Path
    kind: str          # "claude" | "codex" | "grok"
    session_id: str
    config_dir: Path   # the location that offered it (liveness is judged per location)


@dataclass
class OrphanReport:
    found: int = 0
    staged: list[str] = field(default_factory=list)
    known: int = 0
    live: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def find_transcripts(claude_dirs=(), codex_homes=(), grok_homes=()) -> list[Transcript]:
    """Enumerate ONLY the declared locations, ONLY transcript-shaped files."""
    out: list[Transcript] = []
    for c in map(Path, claude_dirs):
        for p in sorted((c / "projects").glob("*/*.jsonl")):
            if UUID_RE.match(p.stem):
                out.append(Transcript(p, "claude", p.stem, c))
    for c in map(Path, codex_homes):
        for p in sorted((c / "sessions").rglob("rollout-*.jsonl")):
            m = CODEX_RE.match(p.name)
            if m:
                out.append(Transcript(p, "codex", m.group(1), c))
    for c in map(Path, grok_homes):
        for p in sorted((c / "sessions").glob("*/chat_history.jsonl")):
            out.append(Transcript(p, "grok", p.parent.name, c))
    return out


def _live_claude_processes() -> list[tuple[Path, float, str | None]]:
    """(config_dir, start_epoch_seconds, resumed_session_id_or_None) for every live claude process."""
    out = []
    try:
        with open("/proc/stat", encoding="ascii") as f:
            boot = float(next(l for l in f if l.startswith("btime")).split()[1])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError):
        return out
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            if not argv or os.path.basename(argv[0].decode(errors="replace")) != "claude":
                continue
            env = dict(kv.split(b"=", 1) for kv in (d / "environ").read_bytes().split(b"\0") if b"=" in kv)
            cfg = env.get(b"CLAUDE_CONFIG_DIR", b"").decode() or str(Path.home() / ".claude")
            start = boot + int((d / "stat").read_text(encoding="ascii", errors="replace").rsplit(")", 1)[1].split()[19]) / hz
            sid = None
            args = [a.decode(errors="replace") for a in argv]
            for flag in ("--resume", "-r"):
                if flag in args and args.index(flag) + 1 < len(args):
                    sid = args[args.index(flag) + 1]
            out.append((Path(cfg).resolve(), start, sid))
        except (OSError, ValueError, IndexError):
            continue
    return out


def is_live(t: Transcript, live=None) -> bool:
    live = _live_claude_processes() if live is None else live
    cfg = t.config_dir.resolve()
    for lcfg, start, sid in live:
        if sid and sid == t.session_id:
            return True
        if t.kind == "claude" and lcfg == cfg:
            try:
                if t.path.stat().st_mtime >= start:
                    return True
            except OSError:
                return True  # cannot judge: treat as live (skip now, the next wake retries)
    return False


def known_hashes(home: Path) -> set[str]:
    """Every content hash the spool has ever recorded for this home (staged AND swept)."""
    out = set()
    d = staging_dir(home)
    if d.is_dir():
        for m in d.glob(f"*{META_SUFFIX}"):
            try:
                out.add(json.loads(m.read_text(encoding="utf-8")).get("sha256", ""))
            except (OSError, ValueError):
                continue
    return out


def first_timestamp(path: Path) -> str:
    """The transcript's own first timestamp (never mtime); '' when none is found (sorts first)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                ts = o.get("timestamp") or (o.get("payload") or {}).get("timestamp") if isinstance(o, dict) else None
                if isinstance(ts, str) and ts:
                    return ts
    except OSError:
        pass
    return ""


def recover(cfg, home: Path, *, claude_dirs=(), codex_homes=(), grok_homes=(), dry_run=False,
            live=None) -> OrphanReport:
    from .spool import SpoolIntegrityError, spool_capture
    from .write_gates import WriteGateError

    rep = OrphanReport()
    seen = known_hashes(home)
    live = _live_claude_processes() if live is None else live
    todo = []
    for t in find_transcripts(claude_dirs, codex_homes, grok_homes):
        rep.found += 1
        if is_live(t, live):
            rep.live.append(t.session_id)
            continue
        try:
            payload = t.path.read_bytes()
        except OSError as e:
            rep.errors.append(f"{t.path}: {e}")
            continue
        if not payload:
            continue
        if hashlib.sha256(payload).hexdigest() in seen:
            rep.known += 1
            continue
        todo.append((first_timestamp(t.path), t, payload))
    for _, t, payload in sorted(todo, key=lambda x: (x[0], str(x[1].path))):
        if dry_run:
            rep.staged.append(t.session_id)
            continue
        try:
            res = spool_capture(cfg, home, payload, session_id=t.session_id,
                                title=f"orphan {t.kind} {t.session_id}", namespace=None)
            if not res.skipped:
                rep.staged.append(t.session_id)
        except (WriteGateError, SpoolIntegrityError) as e:
            rep.errors.append(f"{t.path}: {e}")
    return rep
