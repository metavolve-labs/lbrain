"""A-413 — a generated lair must join the graph, not sit outside it.

`lair-from-repo` emitted Related-Lairs targets in `backticks`. The wikilink
boost only sees [[...]], so every auto-generated lair was a leaf: reachable by
search, invisible to the graph ranking the framework README sells hardest.
Cosmetically it looked linked. That is the failure mode worth pinning — the
document APPEARS cross-referenced while contributing nothing to the graph.
"""
from lbrain.index import WIKILINK_RE
from lbrain.lair_from_repo import DEFAULT_TEMPLATE, build_prompt


def test_template_related_lairs_row_is_a_wikilink():
    assert "[[{other-lair}]]" in DEFAULT_TEMPLATE
    assert "`{other-lair}/`" not in DEFAULT_TEMPLATE


def test_wikilinks_inside_table_cells_are_extracted():
    # The fix is only real if the extractor sees links in a TABLE, since the
    # house style mandates tables and Related Lairs is always one.
    doc = (
        "## Related Lairs\n\n"
        "| Lair | Relationship |\n|------|--------------|\n"
        "| [[some-other-lair]] | Dependency |\n"
        "| [[a/b/nested-lair]] | Sibling |\n"
    )
    assert WIKILINK_RE.findall(doc) == ["some-other-lair", "a/b/nested-lair"]


def test_backticked_target_is_invisible_to_the_graph():
    # The negative control: prove the OLD form really was a no-op, so this
    # test fails loudly if someone reverts the template to backticks.
    old = "| `some-other-lair/` | Dependency |\n"
    assert WIKILINK_RE.findall(old) == []


def test_prompt_forbids_backticked_targets():
    fixed = {"title": "T", "status": "ACTIVE", "priority": "P1", "last_updated": "2026-08-01"}
    facts = {"docs": {}, "repo_path": "/tmp/r", "blockers": [], "deploy_artifacts": [],
             "last_commit_date": "2026-08-01", "basename": "r", "tables": []}
    system, _ = build_prompt(facts, fixed, DEFAULT_TEMPLATE)
    assert "wikilinks" in system.lower()
    assert "A-413" in system
