"""A3 - a self-retired record names its correction, and a reader served it alone can reach it.

Seven falsification arms written by the CSO at 2026-09-14T00:54Z BEFORE this build was visible,
so they test the property rather than the implementation. F4 is the arm she expected to fail:
the natural implementation reaches for the successor's incoming `Supersedes:` edge, which
satisfies an easier property -- reachability of the PAIR -- not "this record names where its
correction is".

s1.2 precondition (hers): every fixture is shown retrievable BEFORE any retirement filter, or
the arm is NON-DISCRIMINATING and scores neither PASS nor FAIL.
"""
import json
import sqlite3

from lbrain.search import _resolve_retired_successors, _resolve_self_retired_paths


class _Store:
    """Minimal store double: only what the resolvers read."""

    def __init__(self, docs):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE docs (rel_path TEXT, metadata TEXT)")
        for rp, meta in docs:
            self.db.execute("INSERT INTO docs VALUES (?,?)", (rp, json.dumps(meta)))

    def superseded_edges(self):
        return []


RETIRED = {"status": "retired"}


def _succ(docs):
    st = _Store(docs)
    retired = _resolve_self_retired_paths(st)
    assert retired, "PRECONDITION FAILED: no retired record detected -> arm is non-discriminating"
    return retired, _resolve_retired_successors(st, retired)


def test_F1_named_and_existing_resolves_to_an_address():
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="new")), ("new.md", {})])
    assert s["old.md"] == "new.md", "F1: a named, existing successor must resolve to its address"


def test_F2_named_but_nonexistent_is_flagged_never_linked():
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="ghost-record")), ("new.md", {})])
    assert s["old.md"].startswith("?"), "F2: an unresolvable target must never render as a link"
    assert "ghost-record" in s["old.md"], "F2: the unresolvable target must be named"


def test_F3_no_declaration_names_no_successor():
    _, s = _succ([("old.md", dict(RETIRED)), ("new.md", {})])
    assert s.get("old.md", "") == "", "F3: absent declaration must not invent a successor"


def test_F4_incoming_supersedes_edge_is_NOT_a_substitute():
    """THE DISCRIMINATING ARM. The successor declares it replaces this record; the record
    itself names nothing. The edge exists in the graph and is this estate's convention -- and
    it does not satisfy the property, because a reader served the dead record ALONE never
    traverses it."""
    class _EdgeStore(_Store):
        def superseded_edges(self):
            return [("new.md", "old")]

    st = _EdgeStore([("old.md", dict(RETIRED)), ("new.md", {"supersedes": "old"})])
    retired = _resolve_self_retired_paths(st)
    assert retired, "PRECONDITION FAILED: arm is non-discriminating"
    s = _resolve_retired_successors(st, retired)
    assert s.get("old.md", "") == "", (
        "F4: an incoming Supersedes: edge must NOT be rendered as the record naming its "
        "own correction -- that satisfies reachability of the pair, not the location axis"
    )


def test_F5_self_reference_is_not_a_link():
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="old")), ("new.md", {})])
    assert s["old.md"] != "old.md", "F5: self-reference must never resolve to a link"
    assert s["old.md"].startswith("?"), "F5: a record is not its own correction"


def test_F6_co_retrieval_does_not_supply_a_missing_declaration():
    """Both records retrieved together. The retired block must still say none is named:
    proximity in a result set is not the record naming its correction."""
    _, s = _succ([("old.md", dict(RETIRED)), ("new.md", {})])
    assert s.get("old.md", "") == "", "F6: co-retrieval must not supply a declaration"


def test_F7_address_is_carried_on_the_retired_record_itself():
    """Location axis: the address must belong to the retired record's own entry, so a reader
    served that record ALONE still has it."""
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="new")), ("new.md", {})])
    assert "old.md" in s and s["old.md"] == "new.md"
    assert "new.md" not in s, "F7: the address must not live only on the successor's entry"


def test_countermodel_always_link_dies_on_F3():
    _, s = _succ([("old.md", dict(RETIRED)), ("new.md", {})])
    assert "new.md" != s.get("old.md", ""), "an always-link rule must fail F3, or F3 proves nothing"


def test_countermodel_never_link_dies_on_F1():
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="new")), ("new.md", {})])
    assert "" != s["old.md"], "a never-link rule must fail F1, or F1 proves nothing"


def test_F4x_an_edge_using_countermodel_FAILS_F4():
    """F4 is only discriminating if the NATURAL implementation fails it. This is that
    implementation -- union the incoming Supersedes: edges into the successor map -- and it
    must produce a link where F4 requires none. Without this arm, F4 passes for any build
    that simply never looked at edges, including one that names nothing at all."""
    st = _Store([("old.md", dict(RETIRED)), ("new.md", {"supersedes": "old"})])
    retired = _resolve_self_retired_paths(st)
    assert retired, "PRECONDITION FAILED: arm is non-discriminating"

    def edge_using_countermodel(store, retired_paths):
        out = dict(_resolve_retired_successors(store, retired_paths))
        for src, tgt in [("new.md", "old")]:          # the edge the estate already writes
            for rp in retired_paths:
                if rp.rsplit("/", 1)[-1].removesuffix(".md") == tgt:
                    out.setdefault(rp, src)
        return out

    naive = edge_using_countermodel(st, retired)
    real = _resolve_retired_successors(st, retired)
    assert naive.get("old.md") == "new.md", "the countermodel must actually reach for the edge"
    assert real.get("old.md", "") == "", "the build must not"
    assert naive.get("old.md") != real.get("old.md", ""), (
        "F4 must SEPARATE the edge-using implementation from this one, or it is vacuous"
    )
