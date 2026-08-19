"""Two-axis evidence grading — see docs/DESIGN-evidence-grading.md.

The property under test is not "the field round-trips". It is that the two axes
stay APART, that an ungraded record is never promoted, and that an unverified
`author:` string cannot buy reliability. Those are the three ways this feature
turns into the laundering it was built to stop.
"""
from __future__ import annotations

from pathlib import Path

from lbrain import grading
from lbrain.index import parse
from lbrain.search import Hit
from lbrain.serve import _header


def _doc(tmp_path: Path, text: str, name: str = "r.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class TestParseEvidence:
    def test_top_level(self):
        assert grading.parse_evidence({"evidence": "observed"}) == grading.OBSERVED

    def test_nested_under_metadata(self):
        """`type:` and `disclosure:` are both written this way in this corpus."""
        assert grading.parse_evidence(
            {"metadata": {"evidence": "sourced"}}
        ) == grading.SOURCED

    def test_case_and_whitespace_normalised(self):
        assert grading.parse_evidence({"evidence": "  Synthesized "}) == grading.SYNTHESIZED

    def test_absent_is_ungraded(self):
        assert grading.parse_evidence({}) == grading.UNGRADED

    def test_unknown_value_falls_to_ungraded_not_through(self, capsys):
        """An author's typo must not mint a grade the ranker cannot reason about."""
        assert grading.parse_evidence({"evidence": "definitely-true"}) == grading.UNGRADED
        assert "UNGRADED" in capsys.readouterr().err

    def test_unreadable_metadata_is_ungraded_not_a_crash(self):
        assert grading.parse_evidence({"metadata": "not-a-dict"}) == grading.UNGRADED


class TestCredibilityLadder:
    def test_strength_order(self):
        assert grading.credibility(grading.OBSERVED) == "1"
        assert grading.credibility(grading.SOURCED) == "2"
        assert grading.credibility(grading.SYNTHESIZED) == "3"

    def test_ungraded_is_six_not_three(self):
        """6 is 'cannot be judged'; 3 is 'possibly true'. Defaulting to 3 would
        assert something about the record that nobody said."""
        assert grading.credibility(grading.UNGRADED) == "6"
        assert grading.credibility("garbage") == "6"


class TestSourceAxisCannotBeBought:
    def test_unverified_caps_at_F_even_for_an_exact_self_claim(self):
        """The laundering guard. An `author:` field is an assertion until a
        signature backs it; promoting an assertion is the whole failure mode."""
        me = "metavolvelabs/csuite/cso/touchstone"
        assert grading.source_grade(me, me, verified=False) == grading.SRC_UNJUDGEABLE

    def test_missing_identities_are_unjudgeable(self):
        assert grading.source_grade("", "someone", verified=True) == grading.SRC_UNJUDGEABLE
        assert grading.source_grade("someone", "", verified=True) == grading.SRC_UNJUDGEABLE

    def test_verified_ladder(self):
        me = "metavolvelabs/csuite/cso/touchstone"
        colleague = "metavolvelabs/csuite/cto/kite"
        outsider = "othercorp/eng/dev/bob"
        assert grading.source_grade(me, me, verified=True) == grading.SRC_SELF
        assert grading.source_grade(colleague, me, verified=True) == grading.SRC_ORG
        assert grading.source_grade(outsider, me, verified=True) == grading.SRC_EXTERNAL

    def test_org_match_is_a_path_segment_not_a_substring(self):
        """`metavolvelabs-evil` is not a member of `metavolvelabs`. A substring
        test would grade it B."""
        me = "metavolvelabs/csuite/cso/touchstone"
        evil = "metavolvelabs-evil/csuite/cso/touchstone"
        assert grading.source_grade(evil, me, verified=True) == grading.SRC_EXTERNAL


class TestTheSchemeIsNotTheOrg:
    """`gcx://acme/x`.split('/') is ['gcx:', '', 'acme', 'x'].

    Dropping the empties leaves `'gcx:'` in position 0 — identical for every gcx
    name that exists — so the org comparison was always true and ANY two verified
    gcx identities graded B. That is org-insider reliability handed to a stranger,
    the exact laundering this module's docstring says it prevents, defeated by the
    scheme prefix rather than by the substring case it does guard.

    The existing ladder test used bare paths (`a/b` vs `a/c`), so it passed
    throughout — the assertion was satisfied in a form that could not see the bug.
    Dormant only while every caller passes verified=False; it activates the day
    the binding lands, which is the day it would matter most.
    """

    def test_two_different_orgs_under_gcx_are_NOT_the_same_org(self):
        assert grading.source_grade(
            "gcx://metavolvelabs/labs/cso/tad", "gcx://evilcorp/x/y/z",
            verified=True) == grading.SRC_EXTERNAL

    def test_the_same_org_under_gcx_still_grades_B(self):
        """The fix must not simply refuse everything."""
        assert grading.source_grade(
            "gcx://metavolvelabs/labs/cso/tad", "gcx://metavolvelabs/gtm/cco/muse",
            verified=True) == grading.SRC_ORG

    def test_a_substring_org_under_gcx_is_still_external(self):
        assert grading.source_grade(
            "gcx://metavolvelabs-evil/a", "gcx://metavolvelabs/b",
            verified=True) == grading.SRC_EXTERNAL

    def test_case_variants_do_not_inherit_standing(self):
        """Exact match can only refuse membership wrongly, which caps at C — the
        safe direction. Folding case would hand a case-squatter the real org's
        standing if the registry ever turns out to be case-sensitive."""
        assert grading.source_grade(
            "gcx://MetavolveLabs/a", "gcx://metavolvelabs/b",
            verified=True) == grading.SRC_EXTERNAL

    def test_a_different_scheme_is_a_different_namespace(self):
        assert grading.source_grade(
            "evil://metavolvelabs/a", "gcx://metavolvelabs/b",
            verified=True) == grading.SRC_EXTERNAL

    def test_bare_paths_still_work(self):
        """The pre-existing form must keep behaving, in both directions."""
        assert grading.source_grade("acme/x", "acme/y", verified=True) == grading.SRC_ORG
        assert grading.source_grade("acme/x", "other/y", verified=True) == grading.SRC_EXTERNAL


class TestPairIsNeverOneNumber:
    def test_pair_renders_both_axes(self):
        assert grading.pair(grading.OBSERVED, grading.SRC_SELF) == "A1"
        assert grading.pair(grading.UNGRADED) == "F6"

    def test_the_two_uncomparable_pairs_are_distinct_strings(self):
        """B1 and A3 must stay distinguishable. Any collapse to one score makes
        them compare, which is the thing the scheme exists to prevent."""
        assert grading.pair(grading.OBSERVED, grading.SRC_ORG) != grading.pair(
            grading.SYNTHESIZED, grading.SRC_SELF
        )


class TestDocParse:
    def test_frontmatter_reaches_the_doc(self, tmp_path):
        p = _doc(tmp_path, "---\ndate: 2026-08-13\nevidence: observed\n---\n\n# R\n\nWe measured it.\n")
        assert parse(p).evidence == grading.OBSERVED

    def test_nested_form_reaches_the_doc(self, tmp_path):
        p = _doc(tmp_path, "---\nmetadata:\n  type: project\n  evidence: synthesized\n---\n\n# R\n\nProbably.\n")
        d = parse(p)
        assert d.evidence == grading.SYNTHESIZED
        assert d.doc_type == "project"

    def test_plain_markdown_is_ungraded(self, tmp_path):
        """A corpus with no frontmatter at all must not acquire a grade."""
        assert parse(_doc(tmp_path, "# R\n\nJust a note.\n")).evidence == grading.UNGRADED


class TestStoreRoundTrip:
    def test_column_survives_upsert(self, tmp_path):
        from lbrain.store import Store

        p = _doc(tmp_path, "---\nevidence: sourced\n---\n\n# R\n\nPer the filing.\n")
        st = Store(tmp_path / "b.db", embedding_dim=8)
        st.upsert_doc(parse(p))
        row = st.db.execute("SELECT evidence FROM docs").fetchone()
        assert row["evidence"] == grading.SOURCED

    def test_migration_adds_the_column_to_an_older_db(self, tmp_path):
        """A 0.1.4 brain must open and gain the column, not fail.

        The pre-grading `docs` shape is created directly, because that is the
        real upgrade path: SCHEMA uses CREATE TABLE IF NOT EXISTS, so an
        existing table is left alone and only the migration can add the column.
        """
        import sqlite3

        from lbrain.store import Store

        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE docs (rel_path TEXT PRIMARY KEY, abs_path TEXT NOT NULL, "
            "title TEXT NOT NULL, doc_hash TEXT NOT NULL, mtime REAL NOT NULL, "
            "is_priority INTEGER NOT NULL DEFAULT 0, doc_type TEXT NOT NULL DEFAULT '', "
            "metadata TEXT NOT NULL DEFAULT '{}', disclosure TEXT NOT NULL DEFAULT '')"
        )
        con.commit()
        con.close()

        st = Store(db, embedding_dim=8)
        cols = {r["name"] for r in st.db.execute("PRAGMA table_info(docs)")}
        assert "evidence" in cols
        # and the migrated brain still takes a write
        st.upsert_doc(parse(_doc(tmp_path, "---\nevidence: observed\n---\n\n# R\n\nSaw it.\n")))
        assert st.db.execute("SELECT evidence FROM docs").fetchone()["evidence"] == grading.OBSERVED


class TestHeaderRendering:
    def _hit(self, **kw) -> Hit:
        base = dict(rel_path="r.md", chunk_idx=0, text="body", title="R", score=0.5)
        base.update(kw)
        return Hit(**base)

    def test_graded_record_shows_word_and_pair(self):
        out = _header(1, self._hit(evidence=grading.OBSERVED), None, staleness_on=False)
        assert "observed (F1)" in out

    def test_source_axis_is_F_until_a_binding_verifies_it(self):
        """Not A. Nothing yet binds a record to an identity."""
        out = _header(1, self._hit(evidence=grading.OBSERVED), None, staleness_on=False)
        assert "(A1)" not in out

    def test_ungraded_record_adds_nothing(self):
        """Byte-identical to pre-grading output. F6 on every line of every
        existing corpus is the `type=?` noise of A-403 in a new badge."""
        graded = self._hit(evidence=grading.UNGRADED)
        assert "F6" not in _header(1, graded, None, staleness_on=False)
        assert "()" not in _header(1, graded, None, staleness_on=False)

    def test_grade_does_not_displace_the_verdict(self):
        out = _header(1, self._hit(evidence=grading.SOURCED), "ADMISSIBLE", staleness_on=False)
        assert "binds" in out and "sourced (F2)" in out
