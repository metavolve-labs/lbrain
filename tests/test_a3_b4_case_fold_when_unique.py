"""B4 (CSO, 2026-09-14T06:15Z): a self-declared successor that differs from the file only by
case was UNRESOLVABLE, although the record exists. F1 says a named, existing record must LINK;
a case-sensitive resolver is a partial instance of F1's own countermodel.

Rule adopted: fold case only when the fold is UNIQUE; otherwise ambiguous -> UNRESOLVABLE.
Exact match always wins. Same resolver serves the forward `Supersedes:` edge, so the mirror
probe (capitalised edge target) is covered by the same arm.
"""
from test_a3_retired_names_its_correction import RETIRED, _Store, _succ

from lbrain.search import _resolve_superseded_paths


def test_B4_case_differing_self_declaration_resolves_when_unique():
    _, s = _succ([("b4-old.md", dict(RETIRED, superseded_by="B4-New")), ("b4-new.md", {})])
    assert s["b4-old.md"] == "b4-new.md", "B4: unique case-fold must LINK (F1)"


def test_B4m_mirror_capitalised_forward_edge_resolves_when_unique():
    class _EdgeStore(_Store):
        def superseded_edges(self):
            return [("b4-new.md", "B4-Old")]
    st = _EdgeStore([("b4-old.md", {}), ("b4-new.md", {"supersedes": "B4-Old"})])
    assert _resolve_superseded_paths(st) == {"b4-old.md"}, "mirror: capitalised edge must retire"


def test_B4x_ambiguous_fold_is_UNRESOLVABLE_never_a_guess():
    """Countermodel for 'always fold': two records differing only by case."""
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="new")),
                  ("New.md", {}), ("NEW.md", {})])
    assert s["old.md"].startswith("?"), "B4x: an ambiguous fold must not pick one"


def test_B4e_exact_match_wins_over_a_fold_candidate():
    _, s = _succ([("old.md", dict(RETIRED, superseded_by="New")),
                  ("New.md", {}), ("new.md", {})])
    assert s["old.md"] == "New.md", "B4e: exact case match must win even when a fold exists"
