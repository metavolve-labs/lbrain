"""Wake-time orphan capture: kill survival, layer (b) (CSO plan v0.1, 2026-10-16; review 2026-10-17T0137Z).

The SessionEnd hook stages a transcript into the capture spool only when the session ENDS. A hard kill
(SIGKILL, power loss, an OOM) never runs it, so the transcript stays on disk (layer L1) but never reaches the
brain. This module runs from a seat's launcher BEFORE the new session starts: it finds the seat's transcripts
that the spool has never seen in their current form and stages them through the normal gated spool
(``spool_capture``: W1/W2 run before any byte lands). The next ``epoch build`` sweeps them into the brain.

What it must never do (CSO §4.2 negative controls), and how:
- another seat's transcript: it scans ONLY the locations the launcher declares for this seat;
- a file that is not a transcript: Claude Code `<cfg>/projects/<project>/<uuid>.jsonl`, Codex
  `<codex>/sessions/**/rollout-*<uuid>.jsonl`, Grok `<grok>/sessions/<id>/chat_history.jsonl`, nothing else; a
  symlink, or a path that resolves outside its declared location, is never followed;
- a LIVE session. Claude Code does not keep its transcript open (measured 2026-10-16), so liveness is read from
  the process table, and the read FAILS CLOSED: if /proc cannot be read, or a matching process's environment
  cannot, nothing is staged from the affected locations and the run reports an error (rc != 0). Per kind
  (claude, codex, grok): a live process that names its session (`--resume <sid>`, `--resume=<sid>`, `-r <sid>`,
  `codex resume <sid>`) protects that session exactly; and conservatively, any transcript in a location a live
  process of that kind uses, modified at or after that process started, is skipped (SessionEnd or the next wake
  takes it). The start time is min(btime + starttime/hz, mtime of /proc/<pid>): the btime sum alone runs late
  (CSO 0137Z: 1 h 50 m late on a live pid; her 2026-09-14 btime-drift lesson);
- an OLDER form of a session (§4.2.5): content that is a prefix of a captured payload of the same session, or no
  larger than it when the payload has been drained after its sweep, is not re-staged;
- the same content twice: identity is the FULL-content sha256 against every spool meta, never mtime.
Order (§4.5): staged in the order of each transcript's first timestamp, never mtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .spool import META_SUFFIX, PAYLOAD_SUFFIX, staging_dir

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CODEX_RE = re.compile(r"^rollout-.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# the env key that names each kind's location, and its default
HOME_KEY = {"claude": ("CLAUDE_CONFIG_DIR", ".claude"), "codex": ("CODEX_HOME", ".codex"), "grok": ("GROK_HOME", ".grok")}


class LivenessUnreadable(Exception):
    """The process table could not be read: liveness is unknown, so nothing may be staged (fail closed)."""


@dataclass
class Transcript:
    path: Path
    kind: str          # "claude" | "codex" | "grok"
    session_id: str
    location: Path     # the declared location that offered it


@dataclass
class LiveProc:
    kind: str
    location: Path | None   # None = this process's location could not be read: protects EVERY location of its kind
    start: float
    session_id: str | None


@dataclass
class OrphanReport:
    found: int = 0
    staged: list[str] = field(default_factory=list)
    known: int = 0
    older: int = 0
    live: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _inside(p: Path, base: Path) -> bool:
    try:
        return not p.is_symlink() and p.resolve().is_relative_to(base.resolve())
    except OSError:
        return False


def find_transcripts(claude_dirs=(), codex_homes=(), grok_homes=()) -> list[Transcript]:
    """Enumerate ONLY the declared locations, ONLY transcript-shaped files, never through a symlink."""
    out: list[Transcript] = []
    for c in map(Path, claude_dirs):
        for p in sorted((c / "projects").glob("*/*.jsonl")):
            if UUID_RE.match(p.stem) and _inside(p, c):
                out.append(Transcript(p, "claude", p.stem, c))
    for c in map(Path, codex_homes):
        for p in sorted((c / "sessions").rglob("rollout-*.jsonl")):
            m = CODEX_RE.match(p.name)
            if m and _inside(p, c):
                out.append(Transcript(p, "codex", m.group(1), c))
    for c in map(Path, grok_homes):
        for p in sorted((c / "sessions").glob("*/chat_history.jsonl")):
            if _inside(p, c):
                out.append(Transcript(p, "grok", p.parent.name, c))
    return out


def _kind_of(args: list[str]) -> str | None:
    a0 = os.path.basename(args[0]) if args else ""
    if a0 == "claude" or any(a.endswith(("claude-code/cli.js", "claude-code/cli.mjs")) for a in args):
        return "claude"
    if a0 == "codex" or any(a.endswith(("codex/bin/codex.js", "@openai/codex/bin/codex")) for a in args):
        return "codex"
    if a0 == "grok" or any(a.endswith(("grok-cli/dist/index.js", "/bin/grok")) for a in args):
        return "grok"
    return None


def _resumed_sid(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a.startswith("--resume="):
            return a.split("=", 1)[1]
        if a in ("--resume", "-r", "resume") and i + 1 < len(args) and UUID_RE.match(args[i + 1]):
            return args[i + 1]
    return None


def _live_processes(proc_root: Path | str | None = None, btime: float | None = None) -> list[LiveProc]:
    """Every live claude / codex / grok process. Raises LivenessUnreadable when the table cannot be read.

    ``proc_root`` (default /proc) and ``btime`` are FUNCTION PARAMETERS ONLY, so the reader itself can be tested
    against a fabricated table (CSO round 2). There is deliberately no environment override: an env var that
    redirects the process table is a one-line fail-open lever that a seat's <home>/env would carry (CSO round 3).
    Location: the kind's env key (CLAUDE_CONFIG_DIR, CODEX_HOME, GROK_HOME) from THE PROCESS'S environment, a
    relative value resolved against the process's own cwd; without the key, the process's own HOME plus the
    default dir. Never the scanner's home or cwd (CSO round 2 #1). If the environment cannot be read, or neither
    the key nor HOME is in it, the location is UNKNOWN (None), which protects every location of that kind."""
    root = Path(proc_root) if proc_root is not None else Path("/proc")
    try:
        if btime is None:
            with open(root / "stat", encoding="ascii") as f:
                btime = float(next(l for l in f if l.startswith("btime")).split()[1])
        hz = os.sysconf("SC_CLK_TCK")
        pids = [d for d in root.iterdir() if d.name.isdigit()]
    except (OSError, StopIteration, ValueError) as e:
        raise LivenessUnreadable(f"{root} unreadable: {e}") from e
    out: list[LiveProc] = []
    for d in pids:
        try:
            args = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
        except FileNotFoundError:
            continue                                    # exited while we looked
        except OSError as e:
            raise LivenessUnreadable(f"{d}/cmdline unreadable: {e}") from e
        kind = _kind_of(args)
        if kind is None:
            continue
        key, default = HOME_KEY[kind]
        try:
            stat_fields = (d / "stat").read_text(encoding="ascii", errors="replace").rsplit(")", 1)[1].split()
            computed = btime + int(stat_fields[19]) / hz
            start = min(computed, d.stat().st_mtime)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError) as e:
            raise LivenessUnreadable(f"{d}/stat unreadable: {e}") from e
        location: Path | None = None
        env: dict | None = {}
        try:
            for kv in (d / "environ").read_bytes().split(b"\0"):
                for want in (key, "HOME"):
                    pre = want.encode() + b"="
                    if kv.startswith(pre):
                        env[want] = kv[len(pre):].decode(errors="replace")
        except FileNotFoundError:
            continue                                     # exited while we looked
        except OSError:
            env = None                                   # environment unreadable: location unknown

        def _absolute(value: str) -> Path | None:
            p = Path(value)
            if not p.is_absolute():
                try:
                    p = Path(os.readlink(d / "cwd")) / p     # the PROCESS's cwd, never the scanner's
                except OSError:
                    return None                          # cwd unreadable or gone: unknown, never a dropped process
            return p.resolve()

        if env is not None:
            if env.get(key):
                location = _absolute(env[key])
            elif env.get("HOME"):
                h = _absolute(env["HOME"])
                location = (h / default).resolve() if h is not None else None
        out.append(LiveProc(kind, location, start, _resumed_sid(args)))
    return out


def is_live(t: Transcript, live: list[LiveProc]) -> bool:
    loc = t.location.resolve()
    for p in live:
        if p.session_id and p.session_id == t.session_id:
            return True
        if p.kind != t.kind:
            continue
        if p.location is None:
            return True                                  # this kind's location unreadable: fail closed
        if p.location == loc:
            try:
                if t.path.stat().st_mtime >= p.start:
                    return True
            except OSError:
                return True
    return False


def _metas(home: Path) -> list[dict]:
    out = []
    d = staging_dir(home)
    if d.is_dir():
        for m in d.glob(f"*{META_SUFFIX}"):
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            meta["_payload"] = d / (m.name[: -len(META_SUFFIX)] + PAYLOAD_SUFFIX)
            out.append(meta)
    return out


def known_hashes(home: Path) -> set[str]:
    return {m.get("sha256", "") for m in _metas(home)}


def is_older_form(payload: bytes, sid: str, metas: list[dict]) -> bool:
    """§4.2.5: content already captured in a LONGER form of the same session."""
    for m in metas:
        if m.get("session_id") != sid:
            continue
        p: Path = m["_payload"]
        if p.is_file():
            try:
                with open(p, "rb") as f:
                    head = f.read(len(payload) + 1)
            except OSError:
                continue
            if len(head) > len(payload) and head.startswith(payload):
                return True
        elif int(m.get("size") or 0) >= len(payload):
            return True                                   # payload drained after sweep: judge by size, conservatively
    return False


def first_timestamp(path: Path) -> str:
    """The transcript's own first timestamp (never mtime); '' when none is found (sorts first)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                ts = o.get("timestamp") or (o.get("payload") or {}).get("timestamp")
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
    if live is None:
        try:
            live = _live_processes()
        except LivenessUnreadable as e:
            rep.errors.append(f"liveness unknown, nothing staged (fail closed): {e}")
            return rep
    scanned = {k for k, locs in (("claude", claude_dirs), ("codex", codex_homes), ("grok", grok_homes)) if locs}
    for p in live:
        if p.location is None and p.kind in scanned:
            rep.errors.append(f"a live {p.kind} process's location could not be read; every {p.kind} location "
                              "is treated as live (fail closed)")
    metas = _metas(home)
    seen = {m.get("sha256", "") for m in metas}
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
        if is_older_form(payload, t.session_id, metas):
            rep.older += 1
            continue
        todo.append((first_timestamp(t.path), t, payload))
    for _, t, payload in sorted(todo, key=lambda x: (x[0], str(x[1].path))):
        if dry_run:
            rep.staged.append(t.session_id)
            continue
        try:
            res = spool_capture(cfg, home, payload, session_id=t.session_id,
                                # title = the session id, exactly as the SessionEnd hook labels it: archives group a
                                # session's snapshots by title, so a recovered capture must not carry a different one
                                title=t.session_id, namespace=None)
            if not res.skipped:
                rep.staged.append(t.session_id)
        except (WriteGateError, SpoolIntegrityError) as e:
            rep.errors.append(f"{t.path}: {e}")
    return rep
