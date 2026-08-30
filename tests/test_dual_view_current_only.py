"""Dual-view eligibility (DR panel 2026-08-30): current_only EXCLUDES superseded.

Default retrieval flags superseded records but still serves them (naked-stale). The
dual-view fix adds current_only=True on BOTH retrieval paths, which removes closed
records from the result set entirely — a flag the ranker ignores is not a flag.
History/as-of retrieval (current_only=False, the default) still returns + flags them.
Tested on the keyword path (no embedder / API key required).
"""
import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import keyword_only
from lbrain.store import Store

_DOCS = {
    "teamA/status.md":     "# A status\n\nteamA current status content alpha bravo.\n",
    "teamA/status-new.md": "# A status new\n\n**Supersedes:** [[teamA/status]]\n\nteamA new status alpha bravo.\n",
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
        st.replace_supersessions(doc)
    st.db.commit()
    return st


def _paths(hits):
    return {h.rel_path for h in hits}


def test_default_keeps_and_flags_superseded(store):   # NO-REGRESSION
    hits = keyword_only(store, "status", k=10)
    assert "teamA/status.md" in _paths(hits), "default retrieval keeps the superseded record"
    assert any("superseded" in h.boosts for h in hits if h.rel_path == "teamA/status.md"), \
        "and flags it"


def test_current_only_excludes_superseded(store):
    hits = keyword_only(store, "status", k=10, current_only=True)
    assert "teamA/status.md" not in _paths(hits), \
        "current_only must EXCLUDE the superseded record, not merely flag it"
    assert "teamA/status-new.md" in _paths(hits), "the superseding (live) record survives"


def test_current_only_does_not_bury_unrelated(store):   # guard against over-exclusion
    hits = keyword_only(store, "status", k=10, current_only=True)
    assert "teamA/status-new.md" in _paths(hits)
