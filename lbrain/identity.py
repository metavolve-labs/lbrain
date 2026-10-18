"""Identity — who this brain is, and what it is trusted for.

A brain that serves records into an agent's context should be able to say what
it is. Today most of that is local facts (what is indexed, how it serves). The
ecosystem half — a `gcx://` name, a key, and the credentials that name has
earned — arrives with the registry; this module is where it lands, and until
then it reports *unregistered* rather than inventing a status.

Design note, deliberate: an unregistered brain is a first-class state, not an
error. Identity accretes onto an anonymous install; it never gates one.

The record lives at ``~/.lbrain/identity.json`` (0600, same posture as the
secret store — it holds a key).
"""

from __future__ import annotations

import json
import datetime as _dt
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CONFIG_DIR

IDENTITY_PATH = CONFIG_DIR / "identity.json"


@dataclass
class Identity:
    """A registered ecosystem identity. Absent file = unregistered."""

    name: str = ""                          # the gcx:// label, without scheme
    address: str = ""                       # wallet / key address that owns it
    credentials: list[str] = field(default_factory=list)   # verified cred types
    trust_score: float | None = None        # last known bureau score, if any
    registered_at: str = ""                 # ISO date
    issuer: str = ""                        # who attested (never ourselves, for authority)
    # How much of the above was CHECKED. `register` writes plain CLI strings, so the
    # default is the honest one. Only a code path that actually verifies may raise it.
    verification: str = "self-asserted"     # self-asserted | chain-verified

    @property
    def gcx(self) -> str:
        return f"gcx://{self.name}" if self.name else ""

    @classmethod
    def load(cls) -> "Identity | None":
        """The registered identity, or None. Never raises on a damaged file —
        a corrupt identity record must not break retrieval."""
        if not IDENTITY_PATH.exists():
            return None
        try:
            raw = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(
                f"[lbrain] WARNING: {IDENTITY_PATH} is unreadable ({e}); "
                "treating this brain as unregistered.",
                file=sys.stderr,
            )
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass
        body = json.dumps(asdict(self), indent=2).encode("utf-8")
        tmp = IDENTITY_PATH.with_name(IDENTITY_PATH.name + ".tmp")
        # Private from the moment it exists — this record holds a key reference.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, IDENTITY_PATH)


def _serving_db(cfg) -> str:
    from .epoch import serving_db_path
    return serving_db_path(cfg)


_COUNT_KEYS = ("docs", "chunks", "embedded")


def read_counts(cfg) -> tuple[dict, str]:
    """Read the store counts through THE SAME entry point `lbrain stats` uses.

    Returns ``(stats, reason)``: ``reason`` is empty when the counts were read, and
    otherwise names the failure. `whoami` and `lair_whoami` used to wrap this in a bare
    ``except Exception: pass`` and then print the missing keys as Python ``None``
    (CCO home, 2026-10-17: ``None docs`` beside a `stats` of 1543 docs on the same
    epoch). A trust command that swallows its own failure prints a value that looks
    like data and names no cause.
    """
    from .epoch import open_store
    try:
        store = open_store(cfg)
    except Exception as e:  # noqa: BLE001 -- reported, never swallowed
        return {}, f"store did not open: {type(e).__name__}: {e}"
    try:
        return store.stats(), ""
    except Exception as e:  # noqa: BLE001
        return {}, f"count query failed: {type(e).__name__}: {e}"
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 -- the counts (or the reason) are already in hand
            pass


import re as _re

# Key-shaped tokens an exception message may carry (CSO review of PR #87, 2026-10-17: an
# injected `sk-ant-...`, an env line and a newline came out verbatim in whoami). The engine
# has no shared secret scrubber, so this set is deliberately conservative: it redacts more
# than it needs to rather than less.
#
# Name-based rules first (CSO round 2 on #90, 2026-10-18): any NAME containing key, token,
# secret, password, passwd, authorization or credential (case-insensitive), in env
# (`NAME=value`), toml (`name = "value"`), JSON (`"name": "value"`) and query (`?key=value`)
# forms. The NAME survives (it says what kind of thing was there); the value goes, to the
# closing quote when quoted. A name that is an exception TYPE (`KeyError: ...`) is not a
# secret name and is left alone.
#
# Round 3: Python dict repr (`{'api_key': 'v'}`, a quote between the name and the colon),
# URL userinfo (`scheme://user:<pass>@host`, password only; scheme, user and host kept),
# and `pwd`. A name must start at a name boundary and not right after `//`, so the userinfo
# rule's `password:[REDACTED]@host` is not re-read as a name and the host survives.
_SECRET_NAME = (r"(?<!//)(?<![A-Za-z0-9_.\-])"
                r"[A-Za-z0-9_.\-]*(?:key|token|secret|password|passwd|pwd|authorization|credential)"
                r"[A-Za-z0-9_.\-]*")


def _keep_exception_names(repl):
    def sub(m):
        if _re.search(r"(?:error|exception|warning)$", m.group("name"), _re.I):
            return m.group(0)
        return repl(m)
    return sub


# A one-level object or list as a value (CSO r4 on #90): under a secret name the WHOLE
# container goes, whatever its inner names; and a bare value never starts with `{` or `[`,
# so an outer match can no longer swallow an inner `"api_key":` and leave its value behind.
_NESTED = r"\{[^{}]*\}|\[[^\[\]]*\]"
_BARE = r"(?![{\[])"

_SECRET_PATTERNS = (
    # URL userinfo: scheme://user:<password>@host -> the password goes, everything else stays.
    # The password may contain '/' (not RFC-legal unencoded, but seen in the wild).
    (_re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/:@]+):([^\s@]+)@"),
     lambda m: f"{m.group(1)}{m.group(2)}:[REDACTED]@"),
    # HTTP Authorization header, any scheme word (Bearer, token, Basic): the credential goes.
    (_re.compile(r"\b(authorization)\s*[:=]\s*(?:(?:bearer|token|basic)\s+)?[^\s\"',;]+", _re.I),
     lambda m: f"{m.group(1)}: [REDACTED]"),
    # JSON and Python repr: "name": "value", 'name': 'value', mixed quotes, or a bare value
    (_re.compile(r"(?P<nq>[\"'])(?P<name>" + _SECRET_NAME + r")(?P=nq)\s*:\s*"
                 r"(?:(?P<q>[\"'])(?:\\.|(?!(?P=q)).)*(?P=q)|" + _NESTED + "|" + _BARE + r"[^\s,}\]]+)", _re.I),
     _keep_exception_names(lambda m: f"{m.group('nq')}{m.group('name')}{m.group('nq')}: [REDACTED]")),
    # env / toml with a quoted value: NAME = "value with spaces" (or single quotes)
    (_re.compile(r"(?P<name>" + _SECRET_NAME + r")\s*[=:]\s*(?P<q>[\"'])(?:(?!(?P=q)).)*(?P=q)", _re.I),
     _keep_exception_names(lambda m: f"{m.group('name')}=[REDACTED]")),
    # env / query / header with a bare value: NAME=value, ?key=value, X-Auth-Token: value
    (_re.compile(r"(?P<name>" + _SECRET_NAME + r")\s*[=:]\s*(?:" + _NESTED + "|" + _BARE
                 + r"[^\s&\"',;]+)", _re.I),
     _keep_exception_names(lambda m: f"{m.group('name')}=[REDACTED]")),
    (_re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), lambda m: "[REDACTED]"),
    (_re.compile(r"\bBearer\s+\S+", _re.I), lambda m: "Bearer [REDACTED]"),
    (_re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), lambda m: "[REDACTED]"),
    (_re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), lambda m: "[REDACTED]"),
    (_re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), lambda m: "[REDACTED]"),
    (_re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), lambda m: "[REDACTED]"),
    (_re.compile(r"\bAKIA[0-9A-Z]{16}\b"), lambda m: "[REDACTED]"),
    (_re.compile(r"[A-Za-z0-9_\-+]{40,}={0,2}"), lambda m: "[REDACTED]"),   # long opaque token
)
_REASON_CAP = 200


def scrub_reason(text: str) -> str:
    """One display line for an exception text. Order matters (CSO round 2 on #90):
    1. sanitize (serve.sanitize_field: NFKC, single line, control and bidi characters
       stripped), UNCAPPED, so an inserted control, bidi, escape or fullwidth character can
       no longer split a key shape that the strip then rejoins, and no pre-cap cuts a key
       below its pattern length;
    2. redact secret-named values and key-shaped tokens;
    3. cap at about 200 characters, keeping both ends, never splitting a [REDACTED].
    Round 3: every Unicode format character (category Cf: zero-width space and joiners, word
    joiner, BOM, soft hyphen, bidi marks) is stripped first; each is invisible, survives NFKC,
    and one inside a key shape leaked the whole key."""
    from urllib.parse import unquote

    s = _redact(str(text))
    # Percent-encoded forms (`%41Iza...`, `key%3D...`): match on a DECODED copy too, and use it
    # only when it redacts more than the raw text did, so ordinary %-text stays as written.
    dec = unquote(str(text))
    if dec != str(text):
        d = _redact(dec)
        if d.count("[REDACTED]") > s.count("[REDACTED]"):
            s = d
    if len(s) > _REASON_CAP:
        # Keep both ends: the head names the failure class, the tail usually names the cause
        # ("... does not exist"); a long path in the middle is what gets elided.
        head_end = _REASON_CAP * 3 // 10
        tail_start = len(s) - (_REASON_CAP - head_end - 3)
        for m in _PLACEHOLDER.finditer(s):
            if m.start() < head_end < m.end():
                head_end = m.start()        # drop a placeholder the head cut would split
            if m.start() < tail_start < m.end():
                tail_start = m.end()        # likewise at the tail cut
        s = s[:head_end].rstrip() + " … " + s[tail_start:].lstrip()
    return s


_PLACEHOLDER = _re.compile(r"\[REDACTED\]")

# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt). unicodedata does not
# expose the property, so the ranges are listed. Many are category Cf; the rest (U+034F
# combining grapheme joiner, variation selectors, Hangul fillers, ...) are invisible too and
# split a key shape just as well (CSO r4 on #90).
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)


def _invisible(ch: str) -> bool:
    import unicodedata
    if unicodedata.category(ch) == "Cf":
        return True
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _DEFAULT_IGNORABLE)


def _redact(text: str) -> str:
    """Strip invisible characters, sanitize to one line (uncapped), redact."""
    from .serve import sanitize_field
    s = "".join(ch for ch in text if not _invisible(ch))
    s = sanitize_field(s, len(s) + 1)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _counts_unknown(stats: dict | None, counts_error: str) -> str:
    """Empty when every count is a real integer; otherwise the reason it is not (scrubbed,
    one line, capped: it is printed by `whoami` and relayed by `lair_whoami`)."""
    if counts_error:
        return scrub_reason(counts_error)
    missing = [k for k in _COUNT_KEYS if not isinstance((stats or {}).get(k), int)]
    if not missing:
        return ""
    return f"store reported no {', '.join(missing)}"


def _identity_note(ident) -> str:
    """The one line a consuming agent must read before trusting anything above."""
    if ident is None:
        return ("unregistered — local brain, fully functional; no ecosystem "
                "identity claimed")
    if (ident.verification or "self-asserted") != "chain-verified":
        claimed = []
        if ident.credentials:
            claimed.append("credentials")
        if ident.trust_score is not None:
            claimed.append("a trust score")
        if ident.issuer:
            claimed.append(f"an issuer ({ident.issuer})")
        extra = (" It claims " + ", ".join(claimed) + ", none of which were checked."
                 if claimed else "")
        return ("SELF-ASSERTED — this brain declared this identity locally; nothing "
                "verified it against the chain." + extra)
    return ""


def _coverage(cfg) -> tuple[int | None, str]:
    """Measured index coverage from the SERVING epoch (A-576), or (None, "") when
    this epoch predates the measurement.

    Read, never computed here: `whoami` is called on every mount and a real
    coverage survey parses and hashes the whole corpus. `epoch build` already
    walks everything, so it stamps the number in and this only reads it back.

    Absence is reported as absence. An epoch built before this key existed is
    NOT assumed to have full coverage — that assumption is the defect this
    function exists to remove.
    """
    import sqlite3
    db = _serving_db(cfg)
    if not db:
        return None, ""
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        try:
            rows = dict(con.execute(
                "SELECT key, value FROM meta WHERE key IN "
                "('coverage_unscanned','coverage_checked_at')"))
        finally:
            con.close()
    except Exception:
        return None, ""
    if "coverage_unscanned" not in rows:
        return None, ""
    try:
        return int(rows["coverage_unscanned"]), rows.get("coverage_checked_at", "")
    except (TypeError, ValueError):
        return None, ""


def describe(cfg, stats: dict | None = None, counts_error: str = "") -> dict:
    """The structured answer to 'who am I and what am I trusted for?'.

    Returns plain data so the CLI and the MCP tool render the SAME facts — the
    surfaces must not be able to disagree about identity.
    """
    ident = Identity.load()
    _marking = bool(getattr(cfg, "serve_staleness", True))
    _unscanned, _checked_at = _coverage(cfg)
    return {
        "identity": {
            "registered": ident is not None,
            "gcx": ident.gcx if ident else "",
            "address": ident.address if ident else "",
            "credentials": ident.credentials if ident else [],
            "trust_score": ident.trust_score if ident else None,
            # issue #17. `whoami` exists to answer whether a brain "carries any
            # credential beyond its own say-so" — and `register` takes credentials,
            # trust_score and issuer as unvalidated CLI strings. Reporting them with
            # no marker made this surface a trust-laundering primitive: any brain
            # could claim any credential and `lair_whoami` would relay it to another
            # agent as fact. Same rule as gcx.Resolved.status — absence of a check is
            # reported as absence, never as a pass.
            "verification": (ident.verification or "self-asserted") if ident else "",
            "issuer": (ident.issuer or "") if ident else "",
            # An unregistered brain is fully functional. Say so, so an agent
            # reading this does not treat absence as breakage.
            "note": _identity_note(ident),
        },
        "brain": {
            "db": _serving_db(cfg),
            "sources": [str(s) for s in getattr(cfg, "sources", [])],
            "docs": (stats or {}).get("docs"),
            "chunks": (stats or {}).get("chunks"),
            "embedded": (stats or {}).get("embedded"),
            # Empty when the three counts above are real; otherwise WHY they are not, so no
            # surface ever renders a bare None as if it were a count.
            "counts_unknown": _counts_unknown(stats, counts_error),
        },
        # A-585 (2026-09-13): a receipt taken from a LONG-RUNNING process measures the code that
        # process LOADED, not the code on disk. Measured that day: all five live seat MCP children
        # predated the fix they were being formally receipted against -- one by 37 hours -- so every
        # per-seat receipt collected described a process vintage. The trap is that the only way to make
        # such a receipt PASS is to restart the process, which is exactly what an honest seat must not
        # do, because restarting to obtain a pass manufactures the result.
        #
        # The repair is not another rule in a file read at session start: it is that the surface a
        # receipt QUOTES carries its own vintage, so a receipt cannot silently describe the wrong one.
        # A reader comparing `started` against a fix's commit time can see, without asking, whether
        # this process could possibly contain it.
        "runtime": {
            "engine": _engine_stamp_safe(),
            "pid": os.getpid(),
            "started": _proc_started(),
            "note": ("this process loaded its code at 'started'; a fix committed after that time is "
                     "NOT in this process, however current the working tree is"),
        },
        "serving_contract": {
            # What a consumer may rely on when reading this brain's output.
            "mode": getattr(cfg, "serve_mode", "structured"),
            "provider": getattr(cfg, "embedding_provider", "?"),
            "attribution": "every served record carries source, chunk and an honest date label",
            # A-576: this reported `serve_staleness` — a CONFIGURED INTENT — as
            # though it were a measured property. A doc indexed from outside every
            # configured source root is never rescanned, so the build cannot mark
            # it stale no matter what the flag says, and the contract was promising
            # something the mechanism could not deliver (doctrine: default-value is
            # not configured-on is not measured). The flag and the measurement are
            # now reported as two different facts, and an unmeasured epoch says so.
            "staleness_marking_enabled": _marking,
            "staleness_marked": (_marking if _unscanned is None else
                                 bool(_marking and _unscanned == 0)),
            "coverage": ("unverified — this epoch predates the coverage measurement; "
                         "rebuild to measure" if _unscanned is None else
                         {"unscanned_docs": _unscanned, "checked_at": _checked_at,
                          "note": ("every indexed doc is under a configured source root"
                                   if _unscanned == 0 else
                                   f"{_unscanned} indexed doc(s) are under NO configured source "
                                   "root: served, never rescanned, staleness undetectable")}),
            "untrusted_data_fenced": True,
        },
    }


def _proc_started() -> str:
    """UTC ISO start time of THIS process, read from the kernel rather than guessed.

    /proc/self/stat field 22 is start time in clock ticks since boot; boot time comes from
    /proc/stat btime. Falls back to empty rather than to a plausible wrong value -- an invented
    vintage would be worse than none, since the whole point is that a reader can trust it.
    """
    try:
        with open("/proc/self/stat", "rb") as f:
            fields = f.read().rsplit(b")", 1)[1].split()
        ticks = int(fields[19])
        btime = 0
        with open("/proc/stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1]); break
        if not btime:
            return ""
        hz = os.sysconf("SC_CLK_TCK")
        return _dt.datetime.fromtimestamp(btime + ticks / hz,
                                          _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _engine_stamp_safe() -> str:
    try:
        from lbrain.amp import engine_stamp
        return engine_stamp()
    except Exception:
        return ""

