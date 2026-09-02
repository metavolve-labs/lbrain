"""Claim-span dual-view (DR panel 2026-08-30): the grain-mismatch fix.

The document-level dual-view excludes whole superseded DOCS. This tests the finer
grain: a CURRENT document carrying a stale/expired CLAIM. Under current_only, the
chunk containing a closed claim (status != current, or valid_to passed) is dropped;
a chunk with an open/current claim survives. Text-match on the claim, not char offset.
Keyword path (no embedder needed).
"""
import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import keyword_only
from lbrain.store import Store

# Each doc is one chunk; body carries the claim text (frontmatter is stripped pre-chunk).
_DOCS = {
    "stale.md": "---\nclaims:\n  - text: \"burn rate is fifty thousand\"\n    status: superseded\n---\n"
                "# Old finance\nThe burn rate is fifty thousand per the March model. alpha bravo charlie.\n",
    "expired.md": "---\nclaims:\n  - text: \"runway is eighteen months\"\n    valid_to: 2020-01-01\n---\n"
                  "# Runway\nThe runway is eighteen months on the old basis. alpha bravo charlie.\n",
    "live.md": "---\nclaims:\n  - text: \"team size is five\"\n    status: current\n---\n"
               "# Team\nThe team size is five people currently. alpha bravo charlie.\n",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    corpus = tmp_path / "corpus"
    st = Store(tmp_path / "brain.db")
    for rel, txt in _DOCS.items():
        p = corpus / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt)
        doc = parse(corpus / rel, repo_root=corpus)
        st.upsert_doc(doc)
        st.insert_chunks(chunk_doc(doc))
        st.replace_claim_spans(doc)
    st.db.commit()
    return st


def _paths(hits):
    return {h.rel_path for h in hits}


def test_parse_reads_claims_from_frontmatter():
    from pathlib import Path
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "x.md").write_text(_DOCS["stale.md"])
    doc = parse(d / "x.md", repo_root=d)
    assert doc.claims and doc.claims[0]["text"] == "burn rate is fifty thousand"
    assert doc.claims[0]["status"] == "superseded"


def test_closed_claims_classifies_correctly(store):
    closed = store.closed_claims(today="2026-08-30")
    assert "stale.md" in closed and "expired.md" in closed   # superseded + valid_to passed
    assert "live.md" not in closed                           # current, unexpired


def test_default_returns_all(store):   # NO-REGRESSION
    hits = keyword_only(store, "alpha bravo charlie", k=10)
    assert _paths(hits) == {"stale.md", "expired.md", "live.md"}


def test_current_only_drops_closed_claim_chunks(store):
    hits = keyword_only(store, "alpha bravo charlie", k=10, current_only=True)
    assert "stale.md" not in _paths(hits), "chunk with a superseded claim is dropped"
    assert "expired.md" not in _paths(hits), "chunk with an expired (valid_to) claim is dropped"
    assert "live.md" in _paths(hits), "a current claim is not excluded — a fresh span survives"
