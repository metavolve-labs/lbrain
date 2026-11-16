"""FX-031 (2026-11-16): a register id such as C-003 or M-260 must reach FTS5 as the compound phrase it is.

Before: `_fts_query("C-003")` dropped the one-letter half and became `"003"`, so `lbrain search C-003`
returned every row mentioning 003 first. After: the compound is emitted as a quoted phrase ("C 003"),
first, with the single tokens still OR'd behind it for recall.
"""
from lbrain.search import _fts_query


def test_register_id_is_a_phrase_first():
    assert _fts_query("C-003").startswith('"C 003"')
    assert _fts_query("M-260").startswith('"M 260"')
    assert _fts_query("FX-137").startswith('"FX 137"')


def test_compound_inside_a_longer_query_keeps_recall_terms():
    q = _fts_query("A-562 probe")
    assert q.startswith('"A 562"')
    assert '"probe"' in q and '"562"' in q


def test_plain_queries_unchanged_in_shape():
    q = _fts_query("compaction session-load test")
    assert q.startswith('"session load"')          # the compound word first
    assert '"compaction session load test"' in q     # the whole phrase, as before
    assert _fts_query("x") == ""                     # a lone one-letter query is still empty
