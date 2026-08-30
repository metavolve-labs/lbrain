"""Hybrid search: BM25 (FTS5) + cosine (sqlite-vec) fused by Reciprocal Rank Fusion,
then a few cheap, bounded signal boosts (priority, wikilink graph, supersession)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from .config import Config
from .embed import EmbedClient
from .store import Store


@dataclass
class Hit:
    rel_path: str
    chunk_idx: int
    text: str
    title: str
    score: float
    vector_score: float = 0.0
    keyword_score: float = 0.0
    boosts: dict[str, float] = field(default_factory=dict)
    doc_type: str = ""
    is_priority: bool = False
    mtime: float = 0.0
    heading_path: str = ""  # ancestor headings above this chunk (A-513)
    # Evidence class from the doc (lbrain/grading.py). The credibility axis of
    # the served grade; '' = UNGRADED and renders nothing.
    evidence: str = ""
    # Frontmatter `date:` from the doc. The serve path cannot re-derive this from
    # chunk text — the frontmatter is stripped before chunking.
    doc_date: str = ""


# Temporal query signature — triggers the abstraction recency guardrail.
_TEMPORAL_RE = __import__("re").compile(
    r"\b(latest|recent(ly)?|current(ly)?|now|today|newest|most recent|status|"
    r"state of|up[- ]?to[- ]?date|this (week|month)|what changed|updated?)\b",
    __import__("re").IGNORECASE,
)


def _basename_slug(rel_path: str) -> str:
    """Filename stem from a corpus-relative path, on EITHER separator.

    `rsplit("/", 1)` returns the whole path on Windows (backslash separators), so
    the derived slug matched no wikilink target and no supersession edge — the
    graph boost and the superseded de-rank silently no-opped for every Windows
    user. Identical bug to the 000-PRIORITY one fixed in index.py on 2026-07-28;
    this is its sibling, caught by the 2026-07-29 audit (anomaly A-404). A
    ranking difference with no error message, which is worse than a crash.
    """
    parts = [x for x in re.split(r"[\\/]", rel_path) if x]
    name = parts[-1] if parts else rel_path
    stem = name[:-3] if name.endswith(".md") else name
    # A lair is a DIRECTORY whose payload is always `LAIR.md`, and every wikilink
    # to one names the directory (`[[000-PRIORITY-ANOMALY-REGISTER]]`). Deriving
    # the slug from the filename therefore collapsed 164 of 167 lairs onto the
    # single slug "LAIR" — so no wikilink to a lair could ever resolve and no
    # supersession edge naming one could ever match. Measured live 2026-07-30:
    # only 813 of 2,311 wikilink targets (35%) resolved to any doc slug at all
    # (anomaly A-423). For `<DIR>/LAIR.md` the identity is the directory.
    if stem == "LAIR" and len(parts) >= 2:
        return parts[-2]
    return stem


def _dir_of(rel_path: str) -> str:
    parts = [x for x in re.split(r"[\\/]", rel_path) if x]
    return "/".join(parts[:-1])


def _resolve_superseded_paths(store) -> set[str]:
    """Map each supersession edge to the specific rel_path(s) it retires.

    A `Supersedes:` target written as a PATH matches that exact document. A bare
    slug that names exactly one document retires it. A bare slug that COLLIDES
    across directories (AX-06: `teamA/status.md` vs `teamB/status.md`) retires
    only the target in the same directory as the superseding doc; if the collision
    cannot be resolved that way, the edge retires NOTHING — an ambiguous edge must
    never bury an unrelated same-named record.
    """
    edges = store.superseded_edges()
    if not edges:
        return set()
    all_paths = [r["rel_path"] for r in store.db.execute("SELECT rel_path FROM docs")]
    by_slug: dict[str, list[str]] = {}
    for rp in all_paths:
        by_slug.setdefault(canonical_slug(_basename_slug(rp)), []).append(rp)

    resolved: set[str] = set()
    for src_path, tgt in edges:
        if ("/" in tgt) or ("\\" in tgt):  # author wrote a path — match it exactly
            norm = tgt[:-3] if tgt.endswith(".md") else tgt
            exact = [rp for rp in all_paths
                     if rp == tgt or (rp[:-3] if rp.endswith(".md") else rp) == norm
                     or rp.endswith("/" + tgt) or rp.endswith("/" + norm + ".md")]
            if len(exact) == 1:
                resolved.add(exact[0])
                continue
            # fall through to slug resolution if the path form was not unique
        cands = by_slug.get(canonical_slug(tgt), [])
        if len(cands) == 1:
            resolved.add(cands[0])
        elif len(cands) > 1:
            src_dir = _dir_of(src_path)
            same_dir = [c for c in cands if _dir_of(c) == src_dir]
            if len(same_dir) == 1:
                resolved.add(same_dir[0])
            # else: genuinely ambiguous — bury nothing
    return resolved


def canonical_slug(target: str) -> str:
    """Normalize a WIKILINK TARGET to the same slug space as a document path.

    Authors write links the way Obsidian does — as relative paths:
        [[../../000-PRIORITY-AO-STRATEGY/LAIR]]
        [[../../project-insurable-trust-standard-2026-06-07]]
    These were compared LITERALLY against a bare document slug, so they could
    never match. Measured live 2026-07-30: 1,475 of 2,312 targets unresolved, and
    the path-shaped ones dominate that set (anomaly A-423, third cause). The
    wikilink graph boost — the feature the framework README sells hardest — was
    therefore inert for most of the corpus.

    Same rule as a document path: drop directories, drop `.md`, and for a lair
    (`.../<DIR>/LAIR`) take the directory. One slug space, both sides.
    """
    t = (target or "").strip().strip("/")
    if not t:
        return ""
    parts = [x for x in re.split(r"[\\/]", t) if x not in ("", ".", "..")]
    if not parts:
        return ""
    name = parts[-1]
    stem = name[:-3] if name.endswith(".md") else name
    if stem == "LAIR" and len(parts) >= 2:
        return parts[-2]
    return stem


class HitList(list):
    """A list of Hit that also carries what the disclosure envelope REMOVED.

    A plain `list` subclass on purpose: every existing caller — serve, amp,
    rerank, the CLI, the MCP tools — keeps working untouched, while a caller that
    wants to report blinding can read `.withheld`. Threading an out-parameter
    through six call sites to move one integer would have been the larger change
    and the easier one to forget at a site.

    The integer is not cosmetic. An agent handed a silently-thinned corpus does
    not conclude "I am missing context" — it answers confidently from what is
    left. Withholding has to be *legible* or blinding becomes a confabulation
    engine, which is the A-430 shape pointed the other way.
    """

    withheld = None   # disclosure.Withheld | None
    envelope = None   # disclosure.Envelope | None

    @classmethod
    def of(cls, hits, withheld=None, envelope=None) -> "HitList":
        out = cls(hits)
        out.withheld = withheld
        out.envelope = envelope
        return out


def apply_disclosure(store: Store, hits: list, envelope) -> tuple[list, object]:
    """Run hits through the disclosure envelope (lbrain/disclosure.py).

    Called from BOTH retrieval paths, like apply_belief_visibility, and for the
    same reason: a disclosure control enforced on one path is not a control. That
    lesson is already paid for — the SUPERSEDED badge was invisible on the
    keyword path for weeks (A-410), and this leaks corpus rather than a badge.
    """
    from . import disclosure as _d

    if envelope is None:
        return hits, None
    return _d.apply(hits, store.disclosure_classes(), store.belief_states(), envelope)


def check_persona(store: Store, persona: str | None) -> str:
    """Warn — once — when `persona` matches no belief author. Returns the message.

    Wrong values fail CLOSED, which is correct, but they fail closed **silently**:
    `LBRAIN_PERSONA=CTO` sees nothing, and the author concludes their beliefs were
    lost rather than that they mistyped their own name. Recommended by the router
    lane after they proved the closure adversarially (12 hostile values, all
    hidden, each with a negative control).

    Warn rather than case-fold, deliberately. Case-folding invents an equivalence
    nobody asked for — personas could legitimately be case-distinct — whereas a
    notice surfaces the typo and asserts nothing about identity.
    """
    if not persona:
        return ""
    known = store.belief_personas()
    if not known or persona in known:
        return ""
    msg = (
        f"⚠️  persona {persona!r} has authored no beliefs in this brain "
        f"(known: {', '.join(sorted(known))}). You will see NO drafts. "
        "Matching is exact and case-sensitive."
    )
    if not getattr(check_persona, "_warned", set()) or persona not in check_persona._warned:
        check_persona._warned = getattr(check_persona, "_warned", set()) | {persona}
        print(f"[lbrain] {msg}", file=sys.stderr)
    return msg


def apply_belief_visibility(
    store: Store, hits: list[Hit], persona: str | None, *, rank: bool, penalty: float = 0.25
) -> list[Hit]:
    """Draft isolation + lifecycle marking for per-agent beliefs (lbrain/beliefs.py).

    Applied on BOTH retrieval paths. Splitting disclosure across ranked and
    keyword search is how the SUPERSEDED badge went missing from one of them for
    weeks (A-410) — and a leak here is worse than a missing badge, because it
    hands one persona another's unreviewed speculation as if it were corpus.

    - ``draft``        → DROPPED unless ``persona`` is its author. With
                         ``persona=None`` (every call that predates this layer)
                         no draft is visible to anyone, so existing behaviour on
                         a corpus with no beliefs is bit-identical.
    - ``retracted``    → kept and buried, never deleted: the correction is only
                         legible next to what it corrected, and deleting the
                         negative example means the agent regenerates the error.
    - ``needs_review`` → flagged only. It was not withdrawn; something it rests
                         on was. Penalising it would be an unmeasured ranking
                         change (doctrine rule 4), so we say so and let the
                         reader judge.
    - ``promoted``     → flagged, so a reader can tell an agent's conclusion from
                         an observation. That distinction is the entire point.

    ``rank=False`` (keyword path) marks without touching score, matching how
    supersession is annotated there — but the draft DROP still applies, because
    that is disclosure control, not ranking.
    """
    if not hits:
        return hits
    states = store.belief_states()
    if not states:
        return hits
    out: list[Hit] = []
    for h in hits:
        entry = states.get(h.rel_path)
        if entry is None:
            out.append(h)
            continue
        author, state = entry
        if state == "draft":
            if not persona or author != persona:
                continue  # another agent's private working memory — not corpus
            h.boosts["draft"] = 1.0
        elif state == "retracted":
            if rank:
                h.score *= penalty
            h.boosts["retracted"] = penalty if rank else 1.0
        elif state == "needs_review":
            h.boosts["needs_review"] = 1.0
        elif state == "promoted":
            h.boosts["belief"] = 1.0
        out.append(h)
    return out


def _is_abstraction(h: Hit) -> bool:
    """type: abstraction awareness — delegates to disclosure.is_abstraction.

    One implementation, two callers. The ranking guard and the disclosure filter
    must agree about what an abstraction IS; keeping a second copy here is the
    A-423 shape (two callers of one rule that drifted apart, producing a silent
    no-match with no error message)."""
    from .disclosure import is_abstraction

    return is_abstraction(h.doc_type, h.rel_path)


def _assemble_topk(out: list[Hit], k: int, cfg: Config, query: str, recency: bool) -> list[Hit]:
    """Abstraction-aware final assembly (measured 2026-07-11: at ~46% corpus
    share, uncapped abstractions cost recency −0.083 MRR and evicted gold docs
    from the top-k; at low density they are net-safe enrichment).

    Recency guardrail — on temporally-signed queries (regex or explicit
    recency=True), source documents hold the high ground: abstraction hits are
    stably demoted below ALL source hits. Deliberately stricter than a
    timestamp comparison: an abstraction's mtime is its GENERATION time, not
    its content's age, so freshness math would flatter exactly the stale
    summaries this guards against.

    Density cap — at most cfg.abstraction_topk_cap abstraction chunks in the
    final top-k; excess abstractions yield their slots to source chunks.
    """
    guard = getattr(cfg, "abstraction_recency_guard", True)
    if guard and (recency or _TEMPORAL_RE.search(query)):
        demoted = [h for h in out if _is_abstraction(h)]
        if demoted:
            out = [h for h in out if not _is_abstraction(h)] + demoted
            for h in demoted:
                h.boosts["recency_guard"] = 0.0  # marker: demoted below sources

    cap = getattr(cfg, "abstraction_topk_cap", 2)
    if cap < 0:
        return out[:k]
    picked: list[Hit] = []
    n_abs = 0
    for h in out:
        if _is_abstraction(h):
            if n_abs >= cap:
                continue
            n_abs += 1
        picked.append(h)
        if len(picked) >= k:
            break
    return picked


def search(
    cfg: Config,
    store: Store,
    embedder: EmbedClient,
    query: str,
    k: int = 10,
    doc_type: str | None = None,
    priority_only: bool = False,
    rerank: bool = False,
    recency: bool = False,
    persona: str | None = None,
    envelope=None,
    current_only: bool = False,
) -> list[Hit]:
    """Hybrid: vector top-N + BM25 top-N → RRF merge → always-on boosts → top-k.

    Always-on boosts (net-positive in every regime, measured 2026-06-08): priority,
    wikilink graph, supersession de-ranking.

    Call-when-needed (default OFF — both measured per-regime before shipping):
      recency=True — bounded, read-only mtime-freshness lift; use for recency-sensitive
                     queries ("what's the latest on X"). Priority docs exempt.
      rerank=True  — cross-encoder precision pass over the head; use for PRECISE/
                     known-item lookups. Do NOT use for broad/exploratory queries (it
                     hurts multi-doc coverage). No-ops without the lbrain[rerank] backend.

    ``persona`` names the agent asking. It controls DRAFT BELIEF visibility only
    (see apply_belief_visibility): an agent sees its own working memory and no
    one else's. Default None = no drafts at all, so nothing about a corpus
    without beliefs changes.

    ``envelope`` is a disclosure.Envelope (standing permissions + the per-request
    blinding mode). Default None = no disclosure control, i.e. the behaviour that
    predates this layer. Returns a HitList carrying `.withheld` whenever an
    envelope was applied, so the caller can report the blinding rather than serve
    a silently-thinned corpus.
    """
    # 0. Standing scope, before any retrieval work. A caller asking for a
    #    doc_type outside its allowlist gets NOTHING — not everything. A filter
    #    that widens when it cannot be satisfied is how scope becomes decorative,
    #    which is exactly what A-428 found.
    if envelope is not None:
        from . import disclosure as _d

        doc_type, ok = _d.narrow_doc_type(doc_type, envelope)
        if not ok:
            refused = _d.Withheld()
            refused.by_permission = 1
            refused.total = 1
            return HitList.of([], withheld=refused, envelope=envelope)

    over_k = max(k * 4, 40)
    # When abstraction serving is capped, deepen the candidate pool: in an
    # abstraction-dense corpus the SQL-level top-N cut fills with abstraction
    # chunks BEFORE assembly can cap them, evicting source candidates upstream
    # (measured 2026-07-11: one broad query lost its rank-1 gold doc to pool
    # crowding with zero abstractions in the final top-k).
    if getattr(cfg, "abstraction_topk_cap", 2) >= 0:
        over_k *= 2

    # 1. Vector retrieval
    q_vec = embedder.embed_one(query)
    vec_rows = store.db.execute(
        "SELECT v.rowid AS chunk_id, vec_distance_cosine(v.embedding, ?) AS dist, "
        "       c.rel_path, c.chunk_idx, c.text, c.heading_path, d.title, d.is_priority, d.doc_type, "
        "       d.mtime, d.evidence, d.claim_date "
        "FROM vec_chunks v "
        "JOIN chunks c ON c.chunk_id = v.rowid "
        "JOIN docs d ON d.rel_path = c.rel_path "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY dist",
        (q_vec, q_vec, over_k),
    ).fetchall()

    # 2. Keyword retrieval (BM25 via FTS5)
    fts_query = _fts_query(query)
    kw_rows = []
    if fts_query:
        try:
            kw_rows = store.db.execute(
                "SELECT c.chunk_id, fts_chunks.rank AS rank, c.rel_path, c.chunk_idx, c.text, "
                "       c.heading_path, "
                "       d.title, d.is_priority, d.doc_type, d.mtime, d.evidence, d.claim_date "
                "FROM fts_chunks "
                "JOIN chunks c ON c.chunk_id = fts_chunks.rowid "
                "JOIN docs d ON d.rel_path = c.rel_path "
                "WHERE fts_chunks MATCH ? "
                "ORDER BY rank "
                "LIMIT ?",
                (fts_query, over_k),
            ).fetchall()
        except Exception:
            kw_rows = []

    # 3. Reciprocal Rank Fusion. Combine the two ranked lists by ordinal RANK,
    #    not by raw (incompatible) score scales — robust to the min-max
    #    degeneracy where the single closest vector is always forced to 1.0.
    #    Each list contributes 1/(rrf_k + rank); rrf_k (~60) flattens the curve
    #    so consistent top-rankers dominate without any single list overriding
    #    the consensus. vector_score / keyword_score now hold each list's RRF
    #    contribution (for display/debug), not a normalized similarity.
    rrf_k = cfg.rrf_k
    hits: dict[int, Hit] = {}

    def _hit(r) -> Hit:
        cid = r["chunk_id"]
        h = hits.get(cid)
        if h is None:
            h = Hit(
                rel_path=r["rel_path"],
                chunk_idx=r["chunk_idx"],
                text=r["text"],
                title=r["title"],
                score=0.0,
                doc_type=r["doc_type"],
                is_priority=bool(r["is_priority"]),
                mtime=r["mtime"],
                heading_path=r["heading_path"],
                evidence=r["evidence"],
                doc_date=r["claim_date"],
            )
            hits[cid] = h
        return h

    # vec_rows are ORDER BY dist (best first); kw_rows ORDER BY rank (best first).
    for rank, r in enumerate(vec_rows, start=1):
        h = _hit(r)
        c = 1.0 / (rrf_k + rank)
        h.vector_score += c
        h.score += c
    for rank, r in enumerate(kw_rows, start=1):
        h = _hit(r)
        c = 1.0 / (rrf_k + rank)
        h.keyword_score += c
        h.score += c

    # 4. Filters + domain boosts (multiplicative on the fused score)
    out: list[Hit] = []
    for h in hits.values():
        if doc_type and h.doc_type != doc_type:
            continue
        if priority_only and not h.is_priority:
            continue
        if h.is_priority:
            h.score *= cfg.priority_boost
            h.boosts["priority"] = cfg.priority_boost
        out.append(h)

    # 4.5 Belief visibility — draft isolation first, so another persona's private
    #     working memory can never reach a boost, the budget, or the reader.
    check_persona(store, persona)
    out = apply_belief_visibility(
        store, out, persona, rank=True, penalty=getattr(cfg, "supersede_penalty", 0.25)
    )

    # 4.6 Disclosure envelope — standing permissions, then the per-request
    #     blinding. Before every boost and before the budget, so a withheld
    #     record cannot influence ranking or consume a slot it will not fill.
    out, withheld = apply_disclosure(store, out, envelope)

    # 5. Wikilink graph boost — if a hit is wikilinked-to by another hit, lift it
    if out:
        # Count inbound links in the NORMALIZED slug space. Stored targets are raw
        # author text (often a relative path), so a literal SQL equality missed
        # them; normalizing on read fixes existing brains with no re-import.
        counts: dict[str, int] = {}
        for r in store.db.execute("SELECT tgt_slug FROM wikilinks").fetchall():
            cs = canonical_slug(r["tgt_slug"])
            if cs:
                counts[cs] = counts.get(cs, 0) + 1
        for h in out:
            slug = _basename_slug(h.rel_path)
            in_links = counts.get(slug, 0)
            if in_links:
                lift = 1.0 + 0.05 * min(in_links, 5)  # cap influence
                h.score *= lift
                h.boosts["wikilink_inbound"] = lift

    # 5.5 Supersession-aware de-ranking (Zep-inspired). A doc that another doc
    #     explicitly supersedes is BURIED, not deleted — the live truth surfaces
    #     while the original stays retrievable for provenance/audit. This turns
    #     the "amendable, supersede-not-overwrite" convention into actual ranking
    #     behavior: "permanence at the substrate, selectivity at the surface."
    if getattr(cfg, "supersede_aware", True) and out:
        # AX-06: resolve each edge to a SPECIFIC target path, not a bare slug that
        # buries every same-named doc across directories. A collision resolves to
        # the same-directory target; a still-ambiguous edge buries nothing.
        superseded_paths = _resolve_superseded_paths(store)
        if superseded_paths:
            if current_only:
                # Dual-view eligibility (DR panel, 2026-08-30): default-current retrieval
                # EXCLUDES superseded records rather than serving them naked below the fold.
                # A flag the ranker ignores is not a flag — when the retired value and its
                # correction are both in-context, generation serves the stale one 15-40% of
                # the time (MemStrata 2606.26511; Madam-RAG 2504.13079). History/as-of
                # retrieval (current_only=False, the default) still returns them.
                out = [h for h in out if h.rel_path not in superseded_paths]
            else:
                pen = getattr(cfg, "supersede_penalty", 0.25)
                for h in out:
                    if h.rel_path in superseded_paths:
                        h.score *= pen
                        h.boosts["superseded"] = pen

    # 6. Recency (call-when-needed) — bounded mtime freshness for recency-sensitive
    #    queries. READ-ONLY (no salience writes, no feedback loop), priority docs exempt.
    if recency and out:
        import math
        import time as _time

        now = _time.time()
        hl = 120.0 * 86400.0  # 120-day half-life
        for h in out:
            if h.is_priority:
                continue
            if _is_abstraction(h):
                # mtime = generation time, not content age — a freshness lift here
                # would flatter yesterday's synthesis of last month's state.
                continue
            age = max(now - (h.mtime or now), 0.0)
            freshness = math.pow(0.5, age / hl)  # (0,1], 1 = brand new
            factor = 1.0 + 0.15 * (freshness - 0.5)  # bounded ±0.075
            h.score *= factor
            h.boosts["recency"] = round(factor, 3)

    out.sort(key=lambda h: h.score, reverse=True)

    # 7. Cross-encoder rerank (call-when-needed) — joint (query, chunk) precision pass
    #    over the fused head. No-op without a reranker backend installed.
    if rerank and out:
        from .rerank import rerank as _rerank

        out = _rerank(query, out, top_n=30, priority_boost=cfg.priority_boost)

    # 8. Abstraction-aware final assembly: recency guardrail + top-k density cap.
    #    Applied LAST so the guarantees hold regardless of boosts or rerank.
    return HitList.of(
        _assemble_topk(out, k, cfg, query, recency), withheld=withheld, envelope=envelope
    )


def keyword_only(
    store: Store, query: str, k: int = 10, persona: str | None = None, envelope=None,
    current_only: bool = False,
) -> list[Hit]:
    """Pure FTS5 keyword search. No embedding required.

    Enforces the SAME disclosure envelope as the ranked path. If it did not, the
    control would have a documented bypass reachable by one tool call.
    """
    fts_query = _fts_query(query)
    if not fts_query:
        return HitList.of([], envelope=envelope)
    # Over-fetch ONLY when beliefs exist: dropping another persona's drafts after
    # a LIMIT k would silently return fewer than k results, so isolation would
    # look like poor recall. With no beliefs the query and its result are
    # unchanged, which keeps the pre-existing behaviour exactly.
    fetch = k * 4 if (store.belief_states() or envelope is not None) else k
    rows = store.db.execute(
        "SELECT c.chunk_id, fts_chunks.rank AS rank, c.rel_path, c.chunk_idx, c.text, "
                "       c.heading_path, "
        "       d.title, d.is_priority, d.doc_type, d.mtime, d.evidence, d.claim_date "
        "FROM fts_chunks "
        "JOIN chunks c ON c.chunk_id = fts_chunks.rowid "
        "JOIN docs d ON d.rel_path = c.rel_path "
        "WHERE fts_chunks MATCH ? "
        "ORDER BY rank "
        "LIMIT ?",
        (fts_query, fetch),
    ).fetchall()
    hits = [
        Hit(
            rel_path=r["rel_path"],
            chunk_idx=r["chunk_idx"],
            text=r["text"],
            title=r["title"],
            score=-r["rank"],
            keyword_score=-r["rank"],
            doc_type=r["doc_type"],
            is_priority=bool(r["is_priority"]),
            mtime=r["mtime"],
            heading_path=r["heading_path"],
            evidence=r["evidence"],
            doc_date=r["claim_date"],
        )
        for r in rows
    ]
    # Annotate supersession on the keyword path too. The SUPERSEDED badge in the
    # served header is derived from the `boosts` dict, which only the ranked
    # search path populated — so the product's flagship differentiator ("you are
    # reading a record something else has replaced") was invisible on one of the
    # two retrieval paths (anomaly A-410). No RANKING change here: keyword search
    # stays rank-by-FTS-relevance and the penalty is not applied to the score;
    # this marks the record so the reader is told, which is the whole point.
    # C2-08: resolve to SPECIFIC target paths (AX-06's `_resolve_superseded_paths`),
    # the same as the ranked path — NOT a bare basename-slug set. The old
    # `_basename_slug(rel_path) in superseded_slugs()` flagged every same-named doc
    # across directories, so `teamB/status.md` was marked SUPERSEDED by teamA's edge;
    # an ambiguous edge now buries nothing. Flag only (no score multiplier): keyword
    # search stays rank-by-FTS-relevance; this marks the record so the reader is told.
    superseded_paths = _resolve_superseded_paths(store)
    if superseded_paths:
        if current_only:
            # Dual-view eligibility (DR panel 2026-08-30): exclude superseded on the
            # keyword path too, matching the ranked path. History/as-of retrieval uses
            # current_only=False (the default), which keeps + flags them.
            hits = [h for h in hits if h.rel_path not in superseded_paths]
        else:
            for h in hits:
                if h.rel_path in superseded_paths:
                    h.boosts["superseded"] = 1.0   # flag only, not a score multiplier
    # Draft isolation is disclosure control, so it applies here too — the keyword
    # path must not be a way around it. Marking only (rank=False): keyword search
    # stays rank-by-FTS-relevance, exactly as it does for supersession above.
    check_persona(store, persona)
    hits = apply_belief_visibility(store, hits, persona, rank=False)
    hits, withheld = apply_disclosure(store, hits, envelope)
    return HitList.of(hits[:k], withheld=withheld, envelope=envelope)


def _fts_query(q: str) -> str:
    """Sanitize free-text into an FTS5 MATCH expression.

    Tokens become OR'd quoted terms. For 2–6 token queries we ALSO prepend the
    full token sequence as a quoted phrase, so contiguous/exact matches rank
    above scattered single-token hits. This is what recovers hyphen/dot
    compounds: unicode61 indexes ``C2PA-Manifest-Hash`` as the three tokens
    ``c2pa manifest hash``, so the phrase ``"c2pa manifest hash"`` matches it
    exactly while the individual OR'd terms preserve recall.
    """
    import re

    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", q) if len(t) > 1]
    if not toks:
        return ""
    terms = [f'"{t}"' for t in toks]
    if 2 <= len(toks) <= 6:
        terms.insert(0, '"' + " ".join(toks) + '"')
    return " OR ".join(terms)
