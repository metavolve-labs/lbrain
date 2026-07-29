"""Perishable-claim detection — pure, deterministic, no model call, no I/O.

LBrain can tell you when a record was WRITTEN. It cannot tell you whether the
claim inside it is still TRUE. On 2026-07-27 a lair asserting "DELINQUENT on
Delaware franchise tax" — accurate when verified 18 days earlier, false since —
was retrieved correctly, attributed correctly, dated honestly, and served with
the system's strongest trust marker. It nearly entered a published legal
document. Nothing in the pipeline was broken. There was simply no representation
of the fact that the claim had a shelf life.

This module does not detect falsehood; nothing local can. It detects the CLAIM
CLASS — "this record asserts an open state, and nobody has re-verified it in N
days" — and does the date arithmetic. The judgement stays with the human.

Three tiers, deliberately unequal in confidence:

  DECIDABLE   provable from data we already hold. An author-set `verify_by:`
              that has passed. Zero false positives, so it may speak loudly.
  PROTOCOL    the lair contract already mandates `**Status**:` from a closed
              enum and `**Last Updated**: <ISO>`. Contract-backed, high trust.
  HEURISTIC   an open-state token in an EMPHATIC position — bolded, in a table
              cell, or behind a status emoji. Measured at ~1% of source chunks.

Why emphasis rather than English grammar: LAIR_RULES mandates "tables over
prose ... use status emoji as inline cell values", so the live corpus writes
`| ⚠️ **DELINQUENT** |`, which has no verb for a prose detector to find. A
naive keyword list fires on 74.9% of the corpus and is worthless. Reading the
house authoring contract instead of English costs one regex and fires on 1%.
"""
from __future__ import annotations

import datetime
import re

# Open-state vocabulary. A claim in one of these states is, by definition, a
# claim about something still in motion — which is exactly what decays.
_OPEN = (
    r"DELINQUENT|UNPAID|OVERDUE|PAST\s+DUE|PENDING|AWAITING|BLOCKED|ON\s+HOLD|"
    r"UNRESOLVED|UNVERIFIED|UNCONFIRMED|NOT\s+FILED|NOT\s+PAID|NOT\s+YET|"
    r"IN\s+PROGRESS|IN\s+REVIEW|UNDER\s+REVIEW|OUTSTANDING|SUSPENDED|EXPIRED|"
    r"LAPSED|NEEDS\s+REFRESH|TBD|WIP"
)
_EMOJI = r"[⚠✅❌⏸\U0001f504⬜\U0001f6a7⏳️]*"
_EMOJI_REQ = r"[⚠✅❌⏸\U0001f504⬜\U0001f6a7⏳]️?"   # at least one

# The token must be EMPHASISED (**bold**), in a table cell (| ... |), or behind
# a status emoji. A bare mention in prose ("the pending question of...") is not
# a status assertion and must not fire.
# NOTE the emoji quantifiers. In the third branch it is `+`, not `*`: a bare
# token with no emphasis at all matched `"status": "pending"` inside every JSON
# example in the corpus and drove the fire rate to 46%. Emphasis is the signal —
# it is the author flagging a live state, as opposed to merely using the word.
_MARKER = re.compile(
    rf"(?:\*\*\s*{_EMOJI}\s*\**\s*(?:{_OPEN})\b"        # **BOLD** / **⚠️ BOLD**
    rf"|\|\s*{_EMOJI}\s*\**\s*(?:{_OPEN})\b"             # | table cell
    rf"|{_EMOJI_REQ}\s*\**\s*(?:{_OPEN})\b)",              # ⚠️ emoji-marked
    re.IGNORECASE,
)

# Tolerate the markup the house style actually puts in front of these headers.
# Measured on the live corpus 2026-07-29: of 558 `**Status**` occurrences, 122
# (22%) carry a leading `- `, `| ` or `> `, and the old `^\*\*Status\*\*` matched
# NONE of them — so the tier this module documents as "contract-backed, high
# trust" was silently missing nearly a quarter of its input. `**Last Updated**`
# had the same fragility (caught when this file's own register, written with a
# blockquote, failed to produce a `verified` label).
_LEAD = r"[ \t>\-|*]*"
_STATUS_HDR = re.compile(rf"^{_LEAD}\*\*Status\*\*\s*:\s*(.+)$", re.M)
_UPDATED_HDR = re.compile(rf"^{_LEAD}\*\*Last Updated\*\*\s*:\s*(\d{{4}}-\d{{2}}-\d{{2}})", re.M)
_AS_OF = re.compile(r"\bas of\s+(\d{4}-\d{2}-\d{2})", re.I)
_VERIFY_BY = re.compile(r"^verify_by\s*:\s*(\d{4}-\d{2}-\d{2})", re.M | re.I)
_VOLATILE_FALSE = re.compile(r"^volatile\s*:\s*(?:false|no)\s*$", re.M | re.I)
_FN_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Status values from LAIR_RULES that denote work still in flight.
_OPEN_STATUS = ("ACTIVE", "PLANNING", "BLOCKED", "IN PROGRESS", "PENDING")

# Stale by design — these exist to hold superseded material.
_ARCHIVE = ("_archive", "_archive_legacy", "archived-", "/sessions/", "MEMORY-ARCHIVE")

# NOTE: pre-change backup directories deliberately do NOT belong here. Adding
# them (tried 2026-07-28) suppressed the staleness marker on backup copies that
# were still being SERVED alongside the records that superseded them — making
# the superseded text look cleaner than the correction. Silencing the warning
# on something still in the results is worse than no warning at all. The right
# layer is the index: see index.py's exclusion of backup trees.


def is_excluded(rel_path: str) -> bool:
    """Archive paths are stale on purpose; flagging them is pure noise.

    Without this the risk ranking is dominated by `_archive/completed/**`.
    """
    p = rel_path.replace("\\", "/")
    return any(m in p for m in _ARCHIVE)


def claim_date(text: str, rel_path: str, mtime_iso: str = "") -> tuple[str, str]:
    """(label, YYYY-MM-DD) — when the CLAIM was last stood behind, not when the
    file was touched.

    Precedence is by strength of evidence, strongest first:
      `verified`   an explicit `**Last Updated**:` — a human asserting currency
      `as-of`      the newest `as of <ISO>` in the body (abstractions use this)
      `dated`      a date in the filename (the corpus naming convention)
      `file-dated` mtime — a filesystem fact, NOT a claim date. Weakest.

    mtime is last on purpose: it moves when any byte changes, so a typo fix
    makes a two-year-old claim look current. Naming it `file-dated` keeps that
    honest rather than silently flattering.
    """
    m = _UPDATED_HDR.search(text)
    if m:
        return ("verified", m.group(1))
    asof = _AS_OF.findall(text)
    if asof:
        return ("as-of", max(asof))          # newest, not first
    # BOTH separators — see serve.record_date. On Windows this searched the
    # whole path, so a dated PARENT DIRECTORY became the file's claim date.
    m = _FN_DATE.search(re.split(r"[\\/]", rel_path)[-1])
    if m:
        return ("dated", m.group(1))
    return ("file-dated", mtime_iso) if mtime_iso else ("", "")


def open_claims(text: str, limit: int = 3) -> list[str]:
    """The actual asserted claims, trimmed for display.

    A path and an age is a chore. The quoted claim is a decision.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(">"):
            continue
        if s.startswith(("\"", "'", "//", "#!", "|--", "```")) or '":' in s:
            continue                         # JSON/code example, not an assertion
        if _MARKER.search(s):
            out.append(re.sub(r"\s+", " ", s)[:110])
            if len(out) >= limit:
                break
    return out


def volatility(text: str) -> str:
    """'open' if this document asserts a state still in motion, else ''."""
    if _VOLATILE_FALSE.search(text):
        return ""                            # author opt-out, honoured
    m = _STATUS_HDR.search(text)
    if m and any(s in m.group(1).upper() for s in _OPEN_STATUS):
        return "open"
    # Route through the same line filter open_claims uses — searching the raw
    # text matched JSON examples and drove the fire rate to 46%.
    return "open" if open_claims(text, limit=1) else ""


def expired(text: str, today: datetime.date) -> str | None:
    """DECIDABLE tier: an author-set `verify_by:` that has passed.

    No heuristic, no inference — the author named a date and it is behind us.
    """
    m = _VERIFY_BY.search(text)
    if not m:
        return None
    try:
        d = datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None
    return m.group(1) if d < today else None


def days_since(date_str: str, today: datetime.date) -> int | None:
    try:
        return (today - datetime.date.fromisoformat(date_str)).days
    except (ValueError, TypeError):
        return None
