"""Two-axis evidence grading — how well a record is known, and how far the
author can be trusted, held apart on purpose.

See docs/DESIGN-evidence-grading.md.

The failure this exists to prevent: a record asserts, the citation makes the
assertion look checked, nothing checked it. Dating already solved the *when*
half — `staleness.claim_date()` refuses to call an mtime a claim date and names
the weak tier `file-dated` so the weakness survives being read. This is the
*how well known* half, in the same shape.

Two axes, never one number. The Admiralty System (NATO STANAG 2044 / AJP-2.1)
grades source reliability and information credibility independently precisely so
a reliable source cannot validate an uncorroborated claim by association.
Multiplying them back into a single score reintroduces the correlation the
scheme exists to prevent — there is no arithmetic that makes `B1` and `A3`
comparable, and that is the finding, not a gap in it.
"""
from __future__ import annotations

import sys

# ── Axis 2: information credibility (authored) ────────────────────────────────
# Precedence by strength, strongest first — the claim_date() shape.
OBSERVED = "observed"        # the author did / ran / measured / witnessed it
SOURCED = "sourced"          # attributed to a named artifact a reader can check
SYNTHESIZED = "synthesized"  # reasoned, researched or received. not witnessed
EVIDENCE_CLASSES = (OBSERVED, SOURCED, SYNTHESIZED)

UNGRADED = ""                # nothing declared. never silently promoted

_CREDIBILITY = {OBSERVED: "1", SOURCED: "2", SYNTHESIZED: "3"}
CRED_UNGRADED = "6"          # Admiralty 6 — "truth cannot be judged"

# ── Axis 1: source reliability (derived, never authored) ──────────────────────
SRC_SELF = "A"          # the reader's own record
SRC_ORG = "B"           # a verified identity inside the reader's org
SRC_EXTERNAL = "C"      # a verified identity outside it
SRC_UNJUDGEABLE = "F"   # no binding, unsigned, or verification failed


def parse_evidence(meta: dict, path=None) -> str:
    """The declared evidence class, or UNGRADED.

    Accepted at the top level or nested under `metadata:`, matching how `type:`
    and `disclosure:` are already written in this corpus. An unrecognised value
    falls to UNGRADED rather than being passed through — for the same reason
    disclosure does it: an author's typo must not mint a grade the ranker has
    never heard of and therefore cannot reason about. Silence and a typo are
    different facts, so the typo warns.
    """
    raw = ""
    if isinstance(meta.get("metadata"), dict) and meta["metadata"].get("evidence"):
        raw = str(meta["metadata"]["evidence"])
    elif meta.get("evidence"):
        raw = str(meta["evidence"])
    val = raw.strip().lower()
    if val and val not in EVIDENCE_CLASSES:
        print(
            f"[lbrain] WARNING: {path} declares evidence: {raw!r}, which is not one of "
            f"{'/'.join(EVIDENCE_CLASSES)} — treating it as UNGRADED. An ungraded record "
            "is not a graded one; nothing will promote it.",
            file=sys.stderr,
        )
        return UNGRADED
    return val


def credibility(evidence: str) -> str:
    """Evidence class → Admiralty numeric. Unknown/absent → 6, never 3.

    6 is 'truth cannot be judged' and 3 is 'possibly true'. Defaulting an
    ungraded record to 3 would assert something about it that nobody said.
    """
    return _CREDIBILITY.get(evidence, CRED_UNGRADED)


def source_grade(author: str, reader: str, *, verified: bool = False) -> str:
    """Author identity vs the reader's → Admiralty alpha. Derived, never authored.

    ``verified`` is the whole gate. An `author:` field is an ASSERTION until a
    signature backs it, and grading an assertion as B is exactly the laundering
    this module exists to prevent — it would let anyone type their way to
    org-insider reliability. So unverified authorship caps at F, and today every
    honest caller passes verified=False, because nothing yet binds a record to
    an identity and verifies org membership rather than asserting it.

    That is a real dependency, not a stub: the ladder below is inert until the
    binding lands, and it is written out here so the dependency is visible in
    code rather than only in a design document.
    """
    if not verified or not author or not reader:
        return SRC_UNJUDGEABLE
    if author == reader:
        return SRC_SELF
    # gcx:// names are PATHS (org/division/role/name), so org membership is a
    # path-prefix test on segments — not a substring match, which would make
    # `metavolvelabs-evil` a member of `metavolvelabs`.
    a = [s for s in author.split("/") if s]
    r = [s for s in reader.split("/") if s]
    if a and r and a[0] == r[0]:
        return SRC_ORG
    return SRC_EXTERNAL


def pair(evidence: str, source: str = SRC_UNJUDGEABLE) -> str:
    """The displayed grade, e.g. 'A1', 'F6'. Two characters, two facts."""
    return f"{source}{credibility(evidence)}"


def is_graded(evidence: str) -> bool:
    """True when the record declared a class.

    The header suppresses the grade for ungraded records rather than printing
    F6 on every line of every corpus that predates this feature. Precedent:
    `type=?` on unrecognised doc types read as an error in the first output a
    new user saw (A-403), and a plain-markdown corpus has no frontmatter at all.
    Absent is honest; a badge on every record is noise.
    """
    return evidence in EVIDENCE_CLASSES
