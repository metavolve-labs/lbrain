"""Integrator enclave (DR panel 2026-08-30): capability-scoped retrieval, no LLM.

enclave_query returns only chunks under the caller's allowed manifest prefixes — the
query-time fail-closed predicate atop import-time manifests. A scoped caller cannot pull
out-of-manifest content through the union index (confused-deputy defense). current_only
holds; cite_only_out_of_scope strips bytes so cross-compartment conflicts can be reported
by reference without disclosure.
"""
import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import enclave_query
from lbrain.store import Store

_DOCS = {
    "X-STRATEGY-GTM/plan.md":     "# GTM\nThe launch plan is aggressive. alpha bravo charlie.\n",
    "P2-ARTISWA-GALLERY/art.md":  "# Art\nThe gallery piece is luminous. alpha bravo charlie.\n",
    "P2-ARTISWA-GALLERY/old.md":  "---\nclaims:\n  - text: \"the old style\"\n    status: superseded\n---\n"
                                  "# Old\nThe old style is dated. alpha bravo charlie.\n",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    corpus = tmp_path / "corpus"
    st = Store(tmp_path / "brain.db")
    for rel, txt in _DOCS.items():
        p = corpus / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt)
        doc = parse(corpus / rel, repo_root=corpus)
        st.upsert_doc(doc); st.insert_chunks(chunk_doc(doc)); st.replace_claim_spans(doc)
    st.db.commit()
    return st


def _paths(hits):
    return {h.rel_path for h in hits}


def test_scope_excludes_out_of_manifest(store):
    hits = enclave_query(store, "alpha bravo charlie",
                         allowed_prefixes=["P2-ARTISWA-GALLERY"])
    assert "X-STRATEGY-GTM/plan.md" not in _paths(hits), \
        "a CCO-scoped caller must NOT retrieve GTM content through the union index"
    assert "P2-ARTISWA-GALLERY/art.md" in _paths(hits), "in-scope live content is returned"


def test_current_only_holds_inside_scope(store):
    on = enclave_query(store, "alpha bravo charlie",
                       allowed_prefixes=["P2-ARTISWA-GALLERY"], current_only=True)
    assert "P2-ARTISWA-GALLERY/old.md" not in _paths(on), "superseded claim excluded in-scope"
    off = enclave_query(store, "alpha bravo charlie",
                        allowed_prefixes=["P2-ARTISWA-GALLERY"], current_only=False)
    assert "P2-ARTISWA-GALLERY/old.md" in _paths(off), "history mode keeps it"


def test_cite_only_strips_bytes_for_out_of_scope(store):
    hits = enclave_query(store, "alpha bravo charlie",
                         allowed_prefixes=["P2-ARTISWA-GALLERY"],
                         cite_only_out_of_scope=True)
    by_path = {h.rel_path: h for h in hits}
    assert "X-STRATEGY-GTM/plan.md" in by_path, "out-of-scope surfaces as a CITATION"
    gtm = by_path["X-STRATEGY-GTM/plan.md"]
    assert gtm.text == "" and gtm.boosts.get("cite_only") == 1.0, \
        "out-of-scope bytes are stripped — reference only, no disclosure"
    art = by_path["P2-ARTISWA-GALLERY/art.md"]
    assert art.text != "", "in-scope chunk keeps its text"
