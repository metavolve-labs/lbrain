"""`lbrain selftest` — does THIS installed build actually serve correctly, here?

The CSO discovered on day one, by hand, that "I downloaded a brain" does not
guarantee "the brain works." This makes that check a repeatable command.

It ships a tiny golden corpus (as string constants below, so it always travels
with the wheel — no package-data to forget), writes it to a throwaway directory,
indexes it into a throwaway brain through the REAL pipeline
(`parse -> upsert_doc -> chunk -> insert_chunks`/FTS), retrieves with
`keyword_only` (pure FTS5 — no embedder, no API key, no `[local]` extra), renders
through the REAL `render_response`, and asserts the serving INVARIANTS a working
install must uphold.

Why it exists (repo doctrine; exoskeleton ONBOARDING req 13): a green unit suite
proves a mechanism, not that the shipped artifact serves correctly on the user's
machine. `installed != applied` (A-435). This runs the real serve path end to end
and fails loudly if it does not behave — so "the download works" stops being a
first-day discovery and becomes a command anyone can run.

Deterministic, offline, isolated: it never touches the user's real brain.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- the golden corpus: small, and each doc exercises one serving invariant ---

_GOLDEN: dict[str, str] = {
    # Retrieval + honest dating: a **Last Updated** header must serve as a
    # `verified <date>` claim, not the file's mtime (A-402).
    "vantage-cutover.md": (
        "---\nname: vantage-cutover\n---\n"
        "# Vantage-780 cutover\n\n"
        "**Last Updated**: 2026-01-15\n\n"
        "The Vantage-780 deployment processed 780 requests at cutover, "
        "confirmed in the Vantage-780 runbook.\n"
    ),
    # Supersession: the old plan must still be indexed but flagged SUPERSEDED so
    # a reader is told it was replaced ("buried, not deleted").
    "approach-old.md": (
        "---\nname: approach-old\n---\n"
        "# Meridian rollout — original plan\n\n"
        "The Meridian gateway rollout used the batch importer.\n"
    ),
    "approach-new.md": (
        "---\nname: approach-new\n---\n"
        "# Meridian rollout — current plan\n\n"
        "**Supersedes:** [[approach-old]]\n\n"
        "The Meridian gateway rollout uses the streaming importer now.\n"
    ),
    # Staleness: an open-state claim in an emphatic position must carry a
    # perishability marker at the point of use, not only when someone runs a scan.
    "tax-status.md": (
        "---\nname: tax-status\n---\n"
        "# Delaware franchise tax\n\n"
        "**Last Updated**: 2026-01-10\n\n"
        "**Status**: ⚠️ **DELINQUENT** — the Delaware franchise tax is unpaid.\n"
    ),
}


@dataclass
class Invariant:
    name: str
    what: str          # what a working install must do
    ok: bool = False
    detail: str = ""


def _index(corpus_dir: Path, db_path: Path, cfg):
    """Build a throwaway brain via the REAL pipeline. FTS only — no embeddings,
    so no provider or key is required (keyword_only reads FTS5)."""
    from .index import chunk, parse
    from .store import Store

    store = Store(db_path, embedding_dim=cfg.embedding_dim)
    with store.transaction():
        for path in sorted(corpus_dir.glob("*.md")):
            doc = parse(path, repo_root=corpus_dir)
            store.upsert_doc(doc)
            store.replace_supersessions(doc)
            store.replace_wikilinks(doc)
            store.insert_chunks(
                chunk(doc, max_tokens=cfg.chunk_tokens, overlap=cfg.chunk_overlap)
            )
    return store


def _run_invariants(store, cfg) -> list[Invariant]:
    from .search import keyword_only
    from .serve import render_response

    inv: list[Invariant] = [
        Invariant("retrieval", "a query returns the relevant document"),
        Invariant("honest-dating", "a **Last Updated** header serves as `verified <date>`"),
        Invariant("supersession", "a superseded doc is served with the SUPERSEDED flag"),
        Invariant("staleness", "an open-state claim is served with a perishability marker"),
        Invariant("untrusted-fence", "corpus body text is fenced (data, never instructions)"),
    ]
    by = {i.name: i for i in inv}

    # 1. retrieval
    try:
        hits = keyword_only(store, "Vantage-780 cutover requests", k=5)
        i = by["retrieval"]
        # Assert RANK, not mere presence — rank is what C1 was about, and the
        # detail already claims it (CSO review of #13, finding 3).
        i.ok = bool(hits) and hits[0].rel_path == "vantage-cutover.md"
        i.detail = f"{len(hits)} hit(s); top={hits[0].rel_path if hits else '-'}"
    except Exception as e:  # a raised serve path is itself a failed selftest
        by["retrieval"].detail = f"raised {type(e).__name__}: {e}"

    # 2. honest dating + 5. untrusted fence (one render, two invariants)
    try:
        hits = keyword_only(store, "Vantage-780 cutover requests", k=5)
        out = render_response(cfg, hits, "How many requests did the Vantage-780 cutover process?")
        i = by["honest-dating"]
        i.ok = "verified 2026-01-15" in out
        i.detail = "served `verified 2026-01-15`" if i.ok else "claim-date label absent"
        f = by["untrusted-fence"]
        # Assert a CORPUS BODY LINE is actually prefixed with the fence — not that
        # the substring "│ " appears anywhere (the warning banner describes the
        # fence and contains "│", so a substring check passes even with fencing
        # entirely removed — CSO review of #13, finding 1, the security-relevant one).
        f.ok = any(ln.startswith("│ ") and "Vantage-780" in ln for ln in out.splitlines())
        f.detail = "corpus body line is fenced with `│ `" if f.ok else "corpus text NOT fenced"
    except Exception as e:
        by["honest-dating"].detail = f"raised {type(e).__name__}: {e}"
        by["untrusted-fence"].detail = f"raised {type(e).__name__}: {e}"

    # 3. supersession
    try:
        hits = keyword_only(store, "Meridian gateway rollout importer", k=10)
        out = render_response(cfg, hits, "Meridian gateway rollout importer")
        i = by["supersession"]
        i.ok = "SUPERSEDED" in out
        i.detail = "old plan flagged SUPERSEDED" if i.ok else "SUPERSEDED flag absent"
    except Exception as e:
        by["supersession"].detail = f"raised {type(e).__name__}: {e}"

    # 4. staleness
    try:
        hits = keyword_only(store, "Delaware franchise tax status", k=5)
        out = render_response(cfg, hits, "Delaware franchise tax status")
        i = by["staleness"]
        # Pin the real marker shape (`unverified <N>d` / `EXPIRED`), NOT the
        # "unverified (no claim date)" fallback, which is a different claim — an
        # open claim WITH a stale age, not one lacking a date (CSO review, finding 2).
        i.ok = bool(re.search(r"unverified \d+d", out)) or "EXPIRED" in out
        i.detail = "open claim carries a perishability marker" if i.ok else "no stale marker"
    except Exception as e:
        by["staleness"].detail = f"raised {type(e).__name__}: {e}"

    return inv


def run_selftest(as_json: bool = False) -> bool:
    """Index the golden corpus, assert the serving invariants, print a report.

    Returns True iff every invariant held. Isolated in a temp dir + temp DB, so
    it never reads or writes the user's real brain.
    """
    from .config import Config

    cfg = Config()
    cfg.core_memory_path = ""   # no core block — this tests serving, not memory

    with tempfile.TemporaryDirectory(prefix="lbrain-selftest-") as td:
        root = Path(td)
        corpus = root / "corpus"
        corpus.mkdir()
        for name, body in _GOLDEN.items():
            (corpus / name).write_text(body, encoding="utf-8")
        store = _index(corpus, root / "brain.db", cfg)
        try:
            results = _run_invariants(store, cfg)
        finally:
            store.close()

    passed = sum(1 for r in results if r.ok)
    ok = passed == len(results)

    if as_json:
        import json

        print(json.dumps({
            "ok": ok,
            "passed": passed,
            "total": len(results),
            "invariants": [
                {"name": r.name, "ok": r.ok, "what": r.what, "detail": r.detail}
                for r in results
            ],
        }, indent=2))
        return ok

    print("lbrain selftest — does this installed build serve correctly?\n")
    for r in results:
        mark = "✓" if r.ok else "✗"
        print(f"  {mark} {r.name:<16} {r.what}")
        if r.detail:
            print(f"      {r.detail}")
    print()
    if ok:
        print(f"✓ PASS — {passed}/{len(results)} serving invariants held on this build.")
    else:
        print(f"✗ FAIL — {passed}/{len(results)} held. This install does NOT serve "
              f"correctly; the failing invariants above are the symptom.")
    return ok
