"""FROZEN reference: the whoami reason scrub exactly as merged on main at cb51589 (#90).

Used ONLY as the oracle for the no-regression property test (tests/test_scrub_no_regression.py):
every secret fragment this reference hides must stay hidden at the head under test. Do not
"fix" this file; it is a snapshot. The shared `sanitize_field` is imported from the engine.
"""
# flake8: noqa
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


_SECRET_PATTERNS = (
    # URL userinfo: scheme://user:<password>@host -> the password goes, everything else stays.
    (_re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/:@]+):([^\s/@]+)@"),
     lambda m: f"{m.group(1)}{m.group(2)}:[REDACTED]@"),
    # HTTP Authorization header, any scheme word (Bearer, token, Basic): the credential goes.
    (_re.compile(r"\b(authorization)\s*[:=]\s*(?:(?:bearer|token|basic)\s+)?[^\s\"',;]+", _re.I),
     lambda m: f"{m.group(1)}: [REDACTED]"),
    # JSON and Python repr: "name": "value", 'name': 'value', mixed quotes, or a bare value
    (_re.compile(r"(?P<nq>[\"'])(?P<name>" + _SECRET_NAME + r")(?P=nq)\s*:\s*"
                 r"(?:(?P<q>[\"'])(?:\\.|(?!(?P=q)).)*(?P=q)|[^\s,}\]]+)", _re.I),
     _keep_exception_names(lambda m: f"{m.group('nq')}{m.group('name')}{m.group('nq')}: [REDACTED]")),
    # env / toml with a quoted value: NAME = "value with spaces" (or single quotes)
    (_re.compile(r"(?P<name>" + _SECRET_NAME + r")\s*[=:]\s*(?P<q>[\"'])(?:(?!(?P=q)).)*(?P=q)", _re.I),
     _keep_exception_names(lambda m: f"{m.group('name')}=[REDACTED]")),
    # env / query / header with a bare value: NAME=value, ?key=value, X-Auth-Token: value
    (_re.compile(r"(?P<name>" + _SECRET_NAME + r")\s*[=:]\s*[^\s&\"',;]+", _re.I),
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
    import unicodedata

    from lbrain.serve import sanitize_field
    s = "".join(ch for ch in str(text) if unicodedata.category(ch) != "Cf")
    s = sanitize_field(s, len(s) + 1)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
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
