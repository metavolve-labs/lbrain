"""Per-agent belief accumulation — draft isolation, a promotion gate, and the
structure that makes a self-citation loop impossible rather than discouraged.

WHY THIS EXISTS
---------------
A persona with its own brain but no write-back is a costume, not an agent: it
re-derives the same conclusions every session and never gets anything wrong in a
way it can learn from. Letting it write back, naively, is worse — and the reason
is mechanical rather than a matter of discipline.

An agent writes a speculation. Later it retrieves it. Nothing in the retrieved
text distinguishes "I observed this" from "I said this" — both arrive as tokens
with equal standing, and the attention mechanism blends them. It restates the
claim, now with a supporting record. Three restatements read as corroboration.
**The corpus has manufactured a consensus out of one guess.**

Prompting cannot fix that, because the blending happens at retrieval, before any
instruction applies. So the countermeasure has to be structural, and it is this:

    a claim's support is counted by DISTINCT EXTERNAL ROOTS,
    never by the number of records that assert it.

Restating one observation five times yields ``corroboration = 1``. That single
rule is what the evidence-graph traversal below computes, and G2/G3/G4 are what
enforce it.

DESIGN LINEAGE
--------------
- MemTX (arXiv:2607.23929, Li et al., 2026-07-27) — "a memory write is not a
  belief commit." Writes stage in isolation and enter shared truth only through
  validate-and-commit; irreversible actions gate on belief state; retraction
  triggers typed cascading repair. G1–G7 are our validate-and-commit; ``impact:
  action`` is the action-safety class; ``needs_review`` is the cascade, kept
  deliberately non-destructive (see ``cascade_targets``).
- FAVA Trails (github.com/MachineWisdomAI/fava-trails) — Git-native agent
  memory: markdown + YAML frontmatter, draft isolation, promotion via a Trust
  Gate, supersession that HIDES rather than deletes. It converges on what LBrain
  already had; we took the promotion gate and the provenance model, and reused
  our own supersession (``search.py`` §5.5) rather than rebuilding it.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
No network. The gate never dereferences a URL at promotion time — it requires
the AUTHOR to record that they did (``verified:``). A gate that fetched would be
slow, flaky, and would silently start passing when a squatter answered on a dead
domain. Doctrine rule 9 wants the dereference to have happened and to be
attested; it does not want a check that mistakes "something answered" for
"the right thing answered".
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .search import canonical_slug

BELIEF_DOC_TYPE = "belief"

# Lifecycle. `draft` is private to its author; `promoted` is shared truth;
# `retracted` and `needs_review` stay retrievable — burying a corrected belief is
# the point, deleting it destroys the negative example and the agent regenerates
# the same error (same argument index.py makes for keeping superseded text).
STATE_DRAFT = "draft"
STATE_PROMOTED = "promoted"
STATE_RETRACTED = "retracted"
STATE_NEEDS_REVIEW = "needs_review"
STATES = (STATE_DRAFT, STATE_PROMOTED, STATE_RETRACTED, STATE_NEEDS_REVIEW)

# States whose records must never be usable as evidence. A draft is unreviewed
# speculation; a retracted belief is a known error. Citing either is how a loop
# starts, so the traversal refuses both regardless of who authored them.
UNSOUND_EVIDENCE_STATES = (STATE_DRAFT, STATE_RETRACTED)

# Confidence/impact classes that raise the corroboration bar. `action` is
# MemTX's action-safety class: a belief that could gate an irreversible tool call.
CONFIDENCE_HIGH = "high"
IMPACT_ACTION = "action"

DEFAULT_MAX_DEPTH = 3  # hops from a belief to its nearest observation

_EXTERNAL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """One citation. ``kind`` is SYNTACTIC (how it was written), not resolved —
    whether a link points at a doc or at another belief depends on the corpus and
    is decided during traversal, not at parse time.

    ``verified`` records that the AUTHOR dereferenced an external reference. A
    link needs no such flag: resolving it against the corpus is the dereference.
    """

    ref: str
    kind: str  # "link" | "external"
    verified: bool = False

    @property
    def slug(self) -> str:
        return link_slug(self.ref) if self.kind == "link" else ""


@dataclass
class Belief:
    belief_id: str
    rel_path: str
    persona: str = ""
    state: str = STATE_DRAFT
    subject: str = ""
    claim: str = ""
    confidence: str = ""
    impact: str = ""
    created: str = ""
    promoted_at: str = ""
    verify_by: str = ""
    countersigned_by: str = ""
    evidence: list[EvidenceRef] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)

    @property
    def required_roots(self) -> int:
        """How many DISTINCT observations this belief must trace to.

        One for an ordinary claim — most true things in this corpus have exactly
        one source, and demanding two would block "AR was ~$2 on 2026-07-07",
        which is a verified single measurement. Two when the belief asserts high
        confidence or could drive an irreversible action, because those are the
        claims whose failure is expensive.
        """
        if self.confidence.lower() == CONFIDENCE_HIGH or self.impact.lower() == IMPACT_ACTION:
            return 2
        return 1


@dataclass(frozen=True)
class Check:
    code: str
    passed: bool
    detail: str


@dataclass
class GateResult:
    """The promotion verdict. Always carries the evidence it was computed from —
    a gate that says only yes/no is not auditable, and an unauditable gate is the
    thing this module exists to prevent."""

    passed: bool
    checks: list[Check]
    roots: set[str] = field(default_factory=set)
    depth: int | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def report(self) -> str:
        lines = [f"{'PASS' if c.passed else 'FAIL'}  {c.code}  {c.detail}" for c in self.checks]
        lines.append(
            f"roots={len(self.roots)} depth={'-' if self.depth is None else self.depth}"
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# parsing — a belief is a markdown file; the DB is a projection of it
# --------------------------------------------------------------------------


def link_slug(raw: str) -> str:
    """Normalize a link written ANY of the ways an author writes one — bare slug,
    ``[[wikilink]]``, or a relative path — into the single corpus slug space.

    The importer happens to strip `[[ ]]` before storing a Supersedes target, so
    comparing raw text works today by luck. Relying on that luck is the A-423
    failure: two callers of one rule that drifted apart, producing a silent
    no-match with no error message. One rule, used by both sides.
    """
    m = _WIKILINK_RE.search(raw or "")
    return canonical_slug(m.group(1).strip() if m else (raw or ""))


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def parse_evidence(raw) -> list[EvidenceRef]:
    """Frontmatter ``evidence:`` → refs. Accepts three author-friendly forms:

        evidence:
          - "[[some-lair]]"                                  # link
          - "https://turbo.ardrive.io/price/bytes/1048576"   # external, UNVERIFIED
          - {ref: "https://...", verified: 2026-07-31}       # external, attested

    An external reference with no ``verified:`` fails G1 by design. That is
    doctrine rule 9 expressed as a data requirement: the author must state that
    they hit the far side and it answered, rather than the gate assuming a URL
    that merely looks plausible refers to anything.
    """
    out: list[EvidenceRef] = []
    for item in _as_list(raw):
        verified = False
        if isinstance(item, dict):
            ref = str(item.get("ref", "")).strip()
            verified = bool(item.get("verified"))
        else:
            ref = str(item).strip()
        if not ref:
            continue
        if _EXTERNAL_RE.match(ref):
            out.append(EvidenceRef(ref=ref, kind="external", verified=verified))
            continue
        # A link may be written bare, as a wikilink, or as a relative path —
        # link_slug is the one rule both this and G5 go through (A-423).
        m = _WIKILINK_RE.search(ref)
        out.append(EvidenceRef(ref=(m.group(1).strip() if m else ref), kind="link", verified=True))
    return out


def from_doc(doc) -> Belief | None:
    """Project an indexed ``Doc`` into a Belief, or None if it is not one.

    Deliberately tolerant about VALUES and strict about nothing here: every
    required-field check lives in the gate (G6), so a malformed belief still
    imports, still stays visible to its author, and reports precisely why it
    cannot be promoted. Refusing it at import would make it vanish silently,
    which is the failure mode we are trying to end.
    """
    if getattr(doc, "doc_type", "") != BELIEF_DOC_TYPE:
        return None
    meta = getattr(doc, "metadata", {}) or {}
    state = str(meta.get("state", STATE_DRAFT) or STATE_DRAFT).strip().lower()
    if state not in STATES:
        state = STATE_DRAFT  # an unknown lifecycle value is private, never shared
    return Belief(
        belief_id=doc.name_slug,
        rel_path=doc.rel_path,
        persona=str(meta.get("persona", "") or "").strip(),
        state=state,
        subject=str(meta.get("subject", "") or "").strip(),
        claim=str(meta.get("claim", "") or "").strip(),
        confidence=str(meta.get("confidence", "") or "").strip(),
        impact=str(meta.get("impact", "") or "").strip(),
        created=str(meta.get("created", "") or "").strip(),
        promoted_at=str(meta.get("promoted_at", "") or "").strip(),
        verify_by=str(meta.get("verify_by", "") or "").strip(),
        countersigned_by=str(meta.get("countersigned_by", "") or "").strip(),
        evidence=parse_evidence(meta.get("evidence")),
        supersedes=list(getattr(doc, "supersedes", []) or []),
    )


# --------------------------------------------------------------------------
# the corpus view the gate reasons over
# --------------------------------------------------------------------------


class CorpusView(Protocol):
    """What the gate needs to know about the world.

    A narrow protocol so the traversal is testable against a dict without a
    database, and so the gate cannot reach for anything it has not declared.
    """

    def resolve(self, slug: str) -> bool:
        """True if a document with this slug exists in the corpus."""

    def belief(self, slug: str) -> Belief | None:
        """The Belief for this slug, or None if the doc is a plain observation."""

    def author(self, slug: str) -> str:
        """Who wrote the doc at this slug ("" when unattributed)."""

    def promoted_with_subject(self, subject: str) -> list[Belief]:
        """Promoted beliefs already claiming this subject."""


@dataclass
class DictCorpusView:
    """In-memory CorpusView. Used by the tests, and the reference implementation
    of the contract ``StoreCorpusView`` has to satisfy."""

    docs: set[str] = field(default_factory=set)
    beliefs: dict[str, Belief] = field(default_factory=dict)
    authors: dict[str, str] = field(default_factory=dict)

    def resolve(self, slug: str) -> bool:
        return slug in self.docs or slug in self.beliefs

    def belief(self, slug: str) -> Belief | None:
        return self.beliefs.get(slug)

    def author(self, slug: str) -> str:
        b = self.beliefs.get(slug)
        if b is not None:
            return b.persona
        return self.authors.get(slug, "")

    def promoted_with_subject(self, subject: str) -> list[Belief]:
        if not subject:
            return []
        return [
            b
            for b in self.beliefs.values()
            if b.state == STATE_PROMOTED and b.subject == subject
        ]


def from_row(row, evidence_rows: Iterable = ()) -> Belief:
    """Rebuild a Belief from its stored projection (store.belief_row + evidence)."""
    return Belief(
        belief_id=row["belief_id"],
        rel_path=row["rel_path"],
        persona=row["persona"],
        state=row["state"],
        subject=row["subject"],
        claim=row["claim"],
        confidence=row["confidence"],
        impact=row["impact"],
        created=row["created"],
        promoted_at=row["promoted_at"],
        verify_by=row["verify_by"],
        countersigned_by=row["countersigned_by"],
        evidence=[
            EvidenceRef(ref=e["ref"], kind=e["kind"], verified=bool(e["verified"]))
            for e in evidence_rows
        ],
    )


class StoreCorpusView:
    """CorpusView backed by a live Store.

    The doc-slug set is materialized ONCE per instance. A gate walks the same
    corpus many times over one run, and re-deriving slugs per lookup turned an
    O(1) check into a full table scan. Construct a fresh view when the corpus
    changes — it is cheap, and a stale view is a wrong verdict.
    """

    def __init__(self, store, supersedes: dict[str, list[str]] | None = None):
        from .search import _basename_slug

        self._store = store
        self._doc_slugs = {
            _basename_slug(r["rel_path"]): r["rel_path"]
            for r in store.db.execute("SELECT rel_path FROM docs")
        }
        self._beliefs: dict[str, Belief] = {}
        for row in store.belief_rows():
            b = from_row(row, store.belief_evidence_rows(row["belief_id"]))
            # Supersession edges live in their own table (one rule for the whole
            # corpus, not a belief-specific copy) — G5 needs them here.
            if supersedes is None:
                b.supersedes = [
                    r["tgt_slug"]
                    for r in store.db.execute(
                        "SELECT tgt_slug FROM supersessions WHERE src_path = ?",
                        (row["rel_path"],),
                    )
                ]
            else:
                b.supersedes = list(supersedes.get(row["rel_path"], []))
            self._beliefs[b.belief_id] = b

    @property
    def beliefs(self) -> dict[str, Belief]:
        return self._beliefs

    def resolve(self, slug: str) -> bool:
        return slug in self._doc_slugs or slug in self._beliefs

    def belief(self, slug: str) -> Belief | None:
        return self._beliefs.get(slug)

    def author(self, slug: str) -> str:
        b = self._beliefs.get(slug)
        if b is not None:
            return b.persona
        rel = self._doc_slugs.get(slug)
        if not rel:
            return ""
        row = self._store.db.execute(
            "SELECT metadata FROM docs WHERE rel_path = ?", (rel,)
        ).fetchone()
        if row is None:
            return ""
        import json

        try:
            meta = json.loads(row["metadata"] or "{}")
        except (ValueError, TypeError):
            return ""
        return str(meta.get("persona") or meta.get("author") or "")

    def promoted_with_subject(self, subject: str) -> list[Belief]:
        if not subject:
            return []
        return [
            b for b in self._beliefs.values()
            if b.state == STATE_PROMOTED and b.subject == subject
        ]


# --------------------------------------------------------------------------
# evidence-graph traversal — where the self-citation loop actually dies
# --------------------------------------------------------------------------


@dataclass
class Grounding:
    roots: set[str] = field(default_factory=set)          # distinct observations
    root_authors: dict[str, str] = field(default_factory=dict)
    depth: int | None = None                              # hops to nearest root
    unresolved: list[str] = field(default_factory=list)   # cited, does not exist
    unattested: list[str] = field(default_factory=list)   # external, not dereferenced
    unsound: list[str] = field(default_factory=list)      # cited a draft/retracted belief
    cycle: list[str] = field(default_factory=list)        # self-citation loop


def ground(belief: Belief, view: CorpusView, max_depth: int = DEFAULT_MAX_DEPTH) -> Grounding:
    """Breadth-first walk from a belief to the observations underneath it.

    Three properties matter, and they are the whole anti-loop mechanism:

    1. **Roots are a SET.** Ten beliefs restating one lair note contribute one
       root. Corroboration therefore cannot be manufactured by repetition — the
       exact failure the module docstring describes.
    2. **Draft and retracted beliefs are never traversed.** They are recorded as
       ``unsound`` and the walk stops there. An agent cannot bootstrap its own
       unreviewed speculation into support for a promotion, not even indirectly.
    3. **Revisiting a belief is a cycle, not a shortcut.** A → B → A is recorded
       and fails, rather than quietly terminating with A's roots.

    ``depth`` is the SHORTEST distance to any root, so BFS order gives it for
    free. Note it is computed even when it exceeds ``max_depth`` — the gate
    decides what to do with the number; measuring it is this function's job.
    """
    g = Grounding()
    seen: set[str] = {belief.belief_id}
    frontier: list[tuple[Belief, int]] = [(belief, 0)]

    while frontier:
        node, dist = frontier.pop(0)
        for ref in node.evidence:
            if ref.kind == "external":
                if not ref.verified:
                    g.unattested.append(ref.ref)
                    continue
                g.roots.add(ref.ref)
                g.root_authors.setdefault(ref.ref, "")
                g.depth = dist + 1 if g.depth is None else min(g.depth, dist + 1)
                continue

            slug = ref.slug
            if not slug or not view.resolve(slug):
                g.unresolved.append(ref.ref)
                continue

            nxt = view.belief(slug)
            if nxt is None:
                # A plain document: an observation. The walk terminates here,
                # which is what "grounded" means.
                g.roots.add(slug)
                g.root_authors.setdefault(slug, view.author(slug))
                g.depth = dist + 1 if g.depth is None else min(g.depth, dist + 1)
                continue

            if nxt.state in UNSOUND_EVIDENCE_STATES:
                g.unsound.append(f"{slug} ({nxt.state})")
                continue
            if nxt.belief_id in seen:
                g.cycle.append(slug)
                continue
            seen.add(nxt.belief_id)
            frontier.append((nxt, dist + 1))

    return g


# --------------------------------------------------------------------------
# the promotion gate
# --------------------------------------------------------------------------


def _parse_date(s: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def gate(
    belief: Belief,
    view: CorpusView,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    today: datetime.date | None = None,
) -> GateResult:
    """Seven mechanical checks. Every one is a function of the record and the
    corpus — none is a judgement call, so two runs over the same state agree.

    G1 dereference      every citation resolves; external ones are attested
    G2 no loop          no cycle, and no draft/retracted belief in the chain
    G3 grounded         shortest path to an observation is within max_depth
    G4 corroboration    enough DISTINCT roots, ≥1 not written by this persona
    G5 contradiction    a rival promoted belief on the same subject is superseded
    G6 provenance       persona / created / subject / claim / evidence present
    G7 not stale        verify_by has not already passed
    """
    today = today or datetime.date.today()
    checks: list[Check] = []
    g = ground(belief, view, max_depth=max_depth)

    # --- G1: dereference before you reference (doctrine rule 9, applied to memory)
    if g.unresolved or g.unattested:
        bits = []
        if g.unresolved:
            bits.append("unresolved: " + ", ".join(sorted(set(g.unresolved))[:5]))
        if g.unattested:
            bits.append(
                "external not attested (add `verified:`): "
                + ", ".join(sorted(set(g.unattested))[:5])
            )
        checks.append(Check("G1", False, "; ".join(bits)))
    elif not belief.evidence:
        checks.append(Check("G1", False, "no evidence cited"))
    else:
        checks.append(Check("G1", True, f"{len(belief.evidence)} citation(s) dereference"))

    # --- G2: the self-citation loop
    if g.cycle:
        checks.append(
            Check("G2", False, "self-citation cycle via " + ", ".join(sorted(set(g.cycle))[:5]))
        )
    elif g.unsound:
        checks.append(
            Check(
                "G2",
                False,
                "cites unreviewed/withdrawn belief: " + ", ".join(sorted(set(g.unsound))[:5]),
            )
        )
    else:
        checks.append(Check("G2", True, "evidence graph is acyclic and cites no draft"))

    # --- G3: grounded within max_depth
    if g.depth is None:
        checks.append(Check("G3", False, "no path to any observation — speculation only"))
    elif g.depth > max_depth:
        checks.append(Check("G3", False, f"nearest observation is {g.depth} hops (max {max_depth})"))
    else:
        checks.append(Check("G3", True, f"nearest observation {g.depth} hop(s) away"))

    # --- G4: corroboration counted by distinct roots, never by restatement
    need = belief.required_roots
    independent = [r for r in g.roots if g.root_authors.get(r, "") != belief.persona]
    if len(g.roots) < need:
        checks.append(
            Check("G4", False, f"{len(g.roots)} distinct root(s), needs {need}")
        )
    elif not independent:
        checks.append(
            Check("G4", False, f"all {len(g.roots)} root(s) authored by '{belief.persona}'")
        )
    else:
        checks.append(
            Check("G4", True, f"{len(g.roots)} distinct root(s), {len(independent)} independent")
        )

    # --- G5: silent disagreement is what rots a corpus
    rivals = [
        b
        for b in view.promoted_with_subject(belief.subject)
        if b.belief_id != belief.belief_id
    ]
    declared = {link_slug(s) for s in belief.supersedes}
    undeclared = [b.belief_id for b in rivals if b.belief_id not in declared]
    if undeclared:
        checks.append(
            Check(
                "G5",
                False,
                "contradicts promoted belief(s) without a Supersedes edge: "
                + ", ".join(sorted(undeclared)[:5]),
            )
        )
    else:
        checks.append(
            Check("G5", True, f"no undeclared rival on subject '{belief.subject or '-'}'")
        )

    # --- G6: provenance completeness. No plausible defaults — fail loud.
    missing = [
        f
        for f in ("persona", "created", "subject", "claim")
        if not getattr(belief, f)
    ]
    if not belief.evidence:
        missing.append("evidence")
    if missing:
        checks.append(Check("G6", False, "missing: " + ", ".join(missing)))
    else:
        checks.append(Check("G6", True, "persona, created, subject, claim, evidence all present"))

    # --- G7: stale on arrival
    vb = _parse_date(belief.verify_by) if belief.verify_by else None
    if belief.verify_by and vb is None:
        checks.append(Check("G7", False, f"verify_by is not an ISO date: {belief.verify_by!r}"))
    elif vb is not None and vb < today:
        checks.append(Check("G7", False, f"verify_by {vb.isoformat()} already passed"))
    else:
        checks.append(Check("G7", True, "not expired"))

    # --- action-safety class (MemTX): a belief that could drive an irreversible
    #     tool call is never auto-promoted. We refuse rather than invent an
    #     approver — the same reason a confabulated `alerts@` address ate six
    #     months of alerts: a plausible default that wires a side effect is a bug.
    if belief.impact.lower() == IMPACT_ACTION:
        cs = belief.countersigned_by.strip()
        if not cs:
            checks.append(
                Check("G7a", False, "impact: action requires countersigned_by from another lane")
            )
        elif cs == belief.persona:
            checks.append(
                Check("G7a", False, f"countersigned by its own author ('{cs}')")
            )
        else:
            checks.append(Check("G7a", True, f"countersigned by '{cs}'"))

    return GateResult(
        passed=all(c.passed for c in checks), checks=checks, roots=set(g.roots), depth=g.depth
    )


# --------------------------------------------------------------------------
# retraction — cascade, deliberately non-destructive
# --------------------------------------------------------------------------


def cascade_targets(belief_id: str, all_beliefs: Iterable[Belief]) -> list[str]:
    """Promoted beliefs that transitively rest on ``belief_id``.

    MemTX cascades typed REPAIR on retraction. We compute the same closure but
    flag it ``needs_review`` rather than auto-retracting: an automatic
    destructive edit across a belief graph is exactly the unmeasured cascade
    doctrine rule 7 (reversibility first) exists to prevent. The list is the
    finding; a human or the authoring lane decides.

    Draft beliefs are excluded — they are already invisible to everyone else, so
    reviewing them buys nothing until they are proposed for promotion, where the
    gate will re-walk the graph and fail G2 on the retracted node anyway.
    """
    by_id = {b.belief_id: b for b in all_beliefs}
    # child → parents, over LINK evidence only
    parents: dict[str, set[str]] = {}
    for b in by_id.values():
        for ref in b.evidence:
            if ref.kind == "link" and ref.slug:
                parents.setdefault(ref.slug, set()).add(b.belief_id)

    hit: set[str] = set()
    queue = [belief_id]
    while queue:
        cur = queue.pop(0)
        for p in parents.get(cur, ()):
            if p in hit or p == belief_id:
                continue
            b = by_id.get(p)
            if b is None or b.state != STATE_PROMOTED:
                continue
            hit.add(p)
            queue.append(p)
    return sorted(hit)
