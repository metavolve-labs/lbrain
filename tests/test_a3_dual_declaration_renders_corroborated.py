"""A3 annex s6 -- the DUAL-DECLARATION arm (CTO, 2026-09-14, dispositioned under ruling s3).

WHY THIS FILE EXISTS
    The CSO measured (05:10Z) that a record carrying BOTH `superseded_by:` and an incoming
    `Supersedes:` edge rendered bare SUPERSEDED with no address, while the self-only record
    rendered `RETIRED -> corrected by: <addr>`. The corroborated case rendered strictly LESS
    than the uncorroborated one. Ruling s3 (2026-09-14): matching declarations are mutual
    confirmation and the render must prefer the corroborated form; F1 requires the address on
    the old record.

    The defect was not in either resolver -- both were correct -- but in the CALLERS at the
    hybrid and keyword sites, which subtracted the edge set BEFORE resolving successors, so the
    resolver never saw the dual record. Every resolver arm (F1-F7) passed against the broken
    build. A resolver-level test here would have passed before the fix: the accidental-pass
    shape. So these arms go through search() and keyword_only() themselves, then the renderer.

    Reproduced on immutable 0fddb95 with real fixtures (KESTREL dual / OSPREY self-only /
    HARRIER dual-with-ghost) before any code was touched.
"""
from test_ranking import _brain

from lbrain.search import Hit, keyword_only, search
from lbrain.serve import _header

DUAL_OLD = "---\nstatus: superseded\ntitle: KESTREL v1\nsuperseded_by: kestrel-v2\n---\n" \
           "# KESTREL v1\nThe KESTREL deploy flag is --safe-mode.\n"
DUAL_NEW = "---\ntitle: KESTREL v2\n---\n# KESTREL v2\nSupersedes: [[kestrel-v1]]\n" \
           "The KESTREL deploy flag is --verify-first.\n"
SELF_OLD = "---\nstatus: superseded\ntitle: OSPREY v1\nsuperseded_by: osprey-v2\n---\n" \
           "# OSPREY v1\nThe OSPREY flag is --old.\n"
SELF_NEW = "---\ntitle: OSPREY v2\n---\n# OSPREY v2\nThe OSPREY flag is --new.\n"
GHOST_OLD = "---\nstatus: superseded\ntitle: HARRIER v1\nsuperseded_by: harrier-ghost\n---\n" \
            "# HARRIER v1\nThe HARRIER flag is --old.\n"
GHOST_NEW = "---\ntitle: HARRIER v2\n---\n# HARRIER v2\nSupersedes: [[harrier-v1]]\n" \
            "The HARRIER flag is --new.\n"
EDGE_ONLY_OLD = "---\nstatus: superseded\ntitle: MERLIN v1\n---\n# MERLIN v1\nMERLIN is --old.\n"
EDGE_ONLY_NEW = "---\ntitle: MERLIN v2\n---\n# MERLIN v2\nSupersedes: [[merlin-v1]]\nMERLIN is --new.\n"

CORPUS = {"kestrel-v1.md": DUAL_OLD, "kestrel-v2.md": DUAL_NEW,
          "osprey-v1.md": SELF_OLD, "osprey-v2.md": SELF_NEW,
          "harrier-v1.md": GHOST_OLD, "harrier-v2.md": GHOST_NEW,
          "merlin-v1.md": EDGE_ONLY_OLD, "merlin-v2.md": EDGE_ONLY_NEW}


def _both_routes(tmp_path, query):
    cfg, store, emb = _brain(tmp_path, CORPUS)
    return {"hybrid": {h.rel_path: h for h in search(cfg, store, emb, query, k=10)},
            "keyword": {h.rel_path: h for h in keyword_only(store, query, k=10)}}


def _pre(hits, rel):
    assert rel in hits, f"PRECONDITION FAILED: {rel} not retrieved -> arm is non-discriminating"
    return hits[rel]


def test_D1_dual_agreeing_declarations_carry_the_address_and_corroboration(tmp_path):
    for route, hits in _both_routes(tmp_path, "KESTREL").items():
        h = _pre(hits, "kestrel-v1.md")
        assert "superseded" in h.boosts, f"{route}: precondition, the edge must still flag it"
        assert h.retired_successor == "kestrel-v2.md", (
            f"{route}: D1 the dual record must carry its own address (annex s6, F1)")
        assert h.retired_corroborated is True, (
            f"{route}: D1 self-declared successor == edge source is mutual confirmation (ruling s3)")


def test_D2_dual_with_ghost_self_declaration_is_UNRESOLVABLE_never_the_edge(tmp_path):
    """F4 preserved inside the dual case: the incoming edge from harrier-v2 must NOT be
    substituted for the record's own (unresolvable) declaration. The edge never vouches."""
    for route, hits in _both_routes(tmp_path, "HARRIER").items():
        h = _pre(hits, "harrier-v1.md")
        assert h.retired_successor.startswith("?"), f"{route}: D2 ghost must stay unresolvable"
        assert "harrier-ghost" in h.retired_successor, f"{route}: D2 the ghost must be named"
        assert h.retired_corroborated is False, f"{route}: D2 nothing corroborates a ghost"


def test_D3_self_only_record_is_unchanged_by_the_fix(tmp_path):
    for route, hits in _both_routes(tmp_path, "OSPREY").items():
        h = _pre(hits, "osprey-v1.md")
        assert "retired" in h.boosts and "superseded" not in h.boosts, f"{route}: self-only route"
        assert h.retired_successor == "osprey-v2.md", f"{route}: D3 self-only address unchanged"
        assert h.retired_corroborated is False, f"{route}: D3 self-only is not corroborated"


def test_D4_edge_only_record_names_nothing_F4_still_holds(tmp_path):
    for route, hits in _both_routes(tmp_path, "MERLIN").items():
        h = _pre(hits, "merlin-v1.md")
        assert "superseded" in h.boosts
        assert h.retired_successor == "", (
            f"{route}: D4 an incoming edge alone must not become the record naming its successor")
        assert h.retired_corroborated is False


def test_D5_dual_record_is_penalised_ONCE_not_twice(tmp_path):
    """The old comment's stated reason for the subtraction was single penalty. Keep that."""
    cfg, store, emb = _brain(tmp_path, CORPUS)
    hits = {h.rel_path: h for h in search(cfg, store, emb, "KESTREL", k=10)}
    h = _pre(hits, "kestrel-v1.md")
    assert "superseded" in h.boosts and "retired" not in h.boosts, (
        "D5: dual record must carry exactly one penalty flag")


# ---- serve layer: what the reader SEES (CSO location axis, s1.1) ----
def _sup(**kw):
    h = Hit(rel_path="old.md", chunk_idx=0, text="t", title="Old", score=1.0)
    h.boosts = dict(getattr(h, "boosts", {}) or {}, superseded=0.25)
    for k, v in kw.items():
        setattr(h, k, v)
    return _header(1, h, None)


def test_S6_dual_corroborated_renders_address_and_mark():
    out = _sup(retired_successor="new.md", retired_corroborated=True)
    assert "SUPERSEDED" in out, "precondition: the superseded token must render"
    assert "new.md" in out and "corrected by" in out, "S6: the address must be RENDERED"
    assert "corroborated" in out, "S6: mutual confirmation must be visible to the reader"


def test_S7_dual_unresolvable_renders_as_NOT_a_link():
    out = _sup(retired_successor="?ghost")
    assert "UNRESOLVABLE" in out and "ghost" in out and "corrected by" not in out


def test_S8_edge_only_renders_plain_SUPERSEDED_as_before():
    out = _sup()
    assert "SUPERSEDED" in out and "corrected by" not in out and "corroborated" not in out


def test_countermodel_the_pre_fix_caller_FAILS_D1(tmp_path):
    """The build before this fix: subtract the edge set, THEN resolve. Every F-arm passed
    against it. This arm must fail against it, or the suite does not discriminate."""
    from lbrain.search import _resolve_retired_successors, _resolve_self_retired_paths, \
        _resolve_superseded_paths
    cfg, store, emb = _brain(tmp_path, CORPUS)
    sup = _resolve_superseded_paths(store)
    pre_fix_retired = _resolve_self_retired_paths(store) - sup
    pre_fix_succ = _resolve_retired_successors(store, pre_fix_retired)
    assert "kestrel-v1.md" not in pre_fix_succ, (
        "countermodel: the pre-fix caller must lose the dual record's address; if it does "
        "not, D1 was never discriminating")
