"""The frontmatter `date:` tier must reach the SERVED header, not just its unit test.

The bug: `index.parse()` sets `body = post.content` — the document with its YAML
block removed — chunks are cut from that body, and `staleness._FM_DATE` is
anchored to the start of a frontmatter block. So the most PORTABLE claim-date
tier, added so a copied corpus does not reage to its copy day, was invisible to
every chunk. Not only deep ones: every one, including the leading chunk.

It survived because two of three callers pass RAW file text — `lbrain stale`
reads files off disk, and the tier's own unit tests in test_staleness.py hand it
a string with the frontmatter still attached. Both resolved it correctly the
whole time while the path a user actually reads fell through to the filename or
the mtime.

So these tests go through the store and the serve path on purpose. A unit test
of `claim_date` cannot catch this class, because `claim_date` was never wrong.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from lbrain.index import chunk, parse
from lbrain.search import keyword_only
from lbrain.serve import _header, record_date
from lbrain.staleness import normalize_claim_date
from lbrain.store import Store


def _served(tmp_path: Path, files: dict[str, str], query: str = "registry recall"):
    """docs -> store -> keyword hits, i.e. the whole path the bug hid behind."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8")
    st = Store(tmp_path / "b.db", embedding_dim=8)
    for f in sorted(src.glob("*.md")):
        d = parse(f, repo_root=src)
        st.upsert_doc(d)
        st.insert_chunks(chunk(d))
    st.db.commit()
    return {h.rel_path: h for h in keyword_only(st, query, k=20)}


class TestNormalise:
    def test_unquoted_yaml_date_is_a_date_object(self):
        assert normalize_claim_date(datetime.date(2026, 8, 13)) == "2026-08-13"

    def test_datetime_is_truncated_to_the_day(self):
        assert normalize_claim_date(datetime.datetime(2026, 8, 13, 4, 5)) == "2026-08-13"

    def test_quoted_yaml_date_is_a_string(self):
        assert normalize_claim_date("2025-11-17") == "2025-11-17"

    def test_junk_is_not_a_claim_date(self):
        for v in ("", "soon", "13/08/2026", None, 2026, [], {}):
            assert normalize_claim_date(v) == "", v


class TestItReachesTheHeader:
    def test_frontmatter_date_serves_as_dated_not_file_dated(self, tmp_path):
        """The regression. Was `file-dated <today>` on every chunk."""
        hits = _served(tmp_path, {
            "note.md": "---\ndate: 2026-08-13\n---\n\n# N\n\nRegistry recall notes.\n",
        })
        out = _header(1, hits["note.md"], None, staleness_on=False)
        assert "dated 2026-08-13" in out
        assert "file-dated" not in out

    def test_quoted_date_too(self, tmp_path):
        hits = _served(tmp_path, {
            "q.md": "---\ndate: '2025-11-17'\n---\n\n# Q\n\nRegistry recall.\n",
        })
        assert record_date(hits["q.md"]) == ("dated", "2025-11-17")

    def test_a_deep_chunk_gets_it_too(self, tmp_path):
        """The whole point: the date is a DOC fact, so chunk 12 has it as much as
        chunk 0. Carrying it in text could only ever have reached the first."""
        body = "\n\n".join(
            f"## Section {i}\n\n" + ("Registry recall discussion. " * 120)
            for i in range(12)
        )
        hits = _served(tmp_path, {"big.md": f"---\ndate: 2026-01-05\n---\n\n# Big\n\n{body}\n"})
        deep = [h for h in hits.values() if h.chunk_idx > 0]
        assert deep, "fixture did not produce more than one chunk"
        for h in deep:
            assert record_date(h) == ("dated", "2026-01-05"), h.chunk_idx

    def test_stale_marker_can_now_measure_an_age(self, tmp_path):
        """Downstream of the same call: these records reported
        `unverified (no claim date)` because the label was never `dated`."""
        from lbrain.serve import stale_marker

        hits = _served(tmp_path, {
            "s.md": "---\ndate: 2026-01-05\n---\n\n# S\n\n**Status**: ACTIVE — registry recall work.\n",
        })
        mark = stale_marker(hits["s.md"], today=datetime.date(2026, 8, 17))
        assert mark and "no claim date" not in mark


class TestPrecedenceIsUnchanged:
    def test_last_updated_still_outranks_the_frontmatter_date(self, tmp_path):
        hits = _served(tmp_path, {
            "h.md": "---\ndate: 2025-01-01\n---\n\n**Last Updated**: 2026-07-15\n\n"
                    "# H\n\nRegistry recall.\n",
        })
        assert record_date(hits["h.md"]) == ("verified", "2026-07-15")

    def test_frontmatter_date_outranks_a_filename_date(self, tmp_path):
        """Documented order: frontmatter is the more PORTABLE of the two."""
        hits = _served(tmp_path, {
            "note-2020-01-01.md": "---\ndate: 2026-03-09\n---\n\n# N\n\nRegistry recall.\n",
        })
        assert record_date(hits["note-2020-01-01.md"]) == ("dated", "2026-03-09")

    def test_filename_date_still_works_without_frontmatter(self, tmp_path):
        hits = _served(tmp_path, {"note-2026-05-15.md": "# N\n\nRegistry recall.\n"})
        assert record_date(hits["note-2026-05-15.md"]) == ("dated", "2026-05-15")

    def test_no_date_anywhere_is_still_honestly_file_dated(self, tmp_path):
        hits = _served(tmp_path, {"plain.md": "# P\n\nRegistry recall.\n"})
        label, _ = record_date(hits["plain.md"])
        assert label == "file-dated"


class TestStorage:
    def test_column_round_trips(self, tmp_path):
        p = tmp_path / "n.md"
        p.write_text("---\ndate: 2026-08-13\n---\n\n# N\n\nBody.\n", encoding="utf-8")
        st = Store(tmp_path / "b.db", embedding_dim=8)
        st.upsert_doc(parse(p))
        assert st.db.execute("SELECT claim_date FROM docs").fetchone()["claim_date"] == "2026-08-13"

    def test_migration_adds_the_column_to_an_older_db(self, tmp_path):
        import sqlite3

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
        cols = {r["name"] for r in Store(db, embedding_dim=8).db.execute("PRAGMA table_info(docs)")}
        assert {"claim_date", "evidence"} <= cols


class TestAnExistingBrainHeals:
    """A column added by migration starts empty. If `doc_metadata_differs` does
    not know about it, every unchanged file is skipped on every future import and
    the column stays empty forever — the feature would reach only corpora
    imported after it shipped.
    """

    def _brain_with_empty_columns(self, tmp_path):
        p = tmp_path / "n.md"
        p.write_text("---\ndate: 2026-08-13\nevidence: sourced\n---\n\n# N\n\nBody.\n",
                     encoding="utf-8")
        doc = parse(p)
        st = Store(tmp_path / "b.db", embedding_dim=8)
        st.upsert_doc(doc)
        # simulate the pre-feature row: same body hash, projections empty
        st.db.execute("UPDATE docs SET evidence = '', claim_date = ''")
        st.db.commit()
        return st, doc

    def test_stale_projection_is_detected(self, tmp_path):
        st, doc = self._brain_with_empty_columns(tmp_path)
        assert st.doc_metadata_differs(doc), \
            "an unchanged body with an empty projection must still refresh"

    def test_and_refreshes_on_upsert(self, tmp_path):
        st, doc = self._brain_with_empty_columns(tmp_path)
        st.upsert_doc(doc)
        row = st.db.execute("SELECT claim_date, evidence FROM docs").fetchone()
        assert (row["claim_date"], row["evidence"]) == ("2026-08-13", "sourced")

    def test_a_genuinely_unchanged_doc_still_reports_no_difference(self, tmp_path):
        """The counter must not report a refresh on every run forever."""
        st, doc = self._brain_with_empty_columns(tmp_path)
        st.upsert_doc(doc)
        assert not st.doc_metadata_differs(doc)
