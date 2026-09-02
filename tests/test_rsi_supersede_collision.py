"""RSI-PRELIM-1 AX-06 / C2-08 — basename-slug supersession collision, CLASS-level.

teamA/status.md and teamB/status.md share the basename slug 'status'. When
teamA/status-new.md supersedes teamA/status.md, teamB/status.md must NOT be
flagged/penalized on EITHER retrieval path, and teamA/status.md MUST still be.

- AX-06 (cycle-1) fixed the resolver `_resolve_superseded_paths` and the RANKED
  `search` path.
- C2-08 (cycle-2) is the class-level miss: `keyword_only` still matched bare
  basename slugs (`superseded_slugs()` + `_basename_slug`), so the keyword path
  swept teamB even though the resolver knew better. A closed fixture is not a
  closed class — the resolver was tested; the CONSUMER on the keyword path was not.
"""
import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.store import Store

_DOCS_BARESLUG = {
    "teamA/status.md":     "# A status\n\nteamA current status content alpha bravo.\n",
    "teamA/status-new.md": "# A status new\n\n**Supersedes:** [[status]]\n\nteamA new status alpha bravo.\n",
    "teamB/status.md":     "# B status\n\nteamB unrelated status content alpha bravo.\n",
}
# Class variant: the edge is written as a PATH, not a bare slug — must still retire
# exactly teamA/status.md and never sweep teamB, on the keyword path too.
_DOCS_PATHFORM = {
    "teamA/status.md":     "# A status\n\nteamA current status content alpha bravo.\n",
    "teamA/status-new.md": "# A status new\n\n**Supersedes:** [[teamA/status]]\n\nteamA new status alpha bravo.\n",
    "teamB/status.md":     "# B status\n\nteamB unrelated status content alpha bravo.\n",
}


def _build(tmp_path, docs, *, chunks=False):
    home = tmp_path / "home"; home.mkdir()
    corpus = tmp_path / "corpus"
    for rel, txt in docs.items():
        p = corpus / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt)
    st = Store(home / "brain.db")
    for rel in docs:
        doc = parse(corpus / rel, repo_root=corpus)
        st.upsert_doc(doc)
        if chunks:                       # keyword_only queries FTS chunks
            st.insert_chunks(chunk_doc(doc))
        st.replace_supersessions(doc)
    st.db.commit()
    return st


@pytest.fixture()
def store_with_collision(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    return _build(tmp_path, _DOCS_BARESLUG)


@pytest.fixture()
def searchable_collision(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    return _build(tmp_path, _DOCS_BARESLUG, chunks=True)


@pytest.fixture()
def searchable_pathform(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    return _build(tmp_path, _DOCS_PATHFORM, chunks=True)


# --- resolver + ranked identity (AX-06, cycle-1) -------------------------------
def _resolved(st):
    from lbrain.search import _resolve_superseded_paths
    return _resolve_superseded_paths(st)


def test_ax06_resolver_does_not_sweep_teamB(store_with_collision):
    assert "teamB/status.md" not in _resolved(store_with_collision), \
        "unrelated same-basename doc must NOT be buried"


def test_ax06_resolver_still_retires_teamA(store_with_collision):   # NO-REGRESSION
    assert "teamA/status.md" in _resolved(store_with_collision), \
        "the genuinely-superseded original must still be de-ranked"


# --- keyword retrieval path (C2-08, cycle-2 — the class-level closure) ----------
def _kw_flagged(st, query="status"):
    """rel_paths the keyword path flags SUPERSEDED for `query`."""
    from lbrain.search import keyword_only
    hits = keyword_only(st, query, k=10)
    assert hits, "fixture did not return keyword hits — FTS not populated"
    return {h.rel_path for h in hits if "superseded" in h.boosts}


def test_c2_08_keyword_path_does_not_sweep_teamB(searchable_collision):
    assert "teamB/status.md" not in _kw_flagged(searchable_collision), \
        "keyword path swept an unrelated same-basename doc (C2-08)"


def test_c2_08_keyword_path_still_flags_teamA(searchable_collision):   # NO-REGRESSION
    assert "teamA/status.md" in _kw_flagged(searchable_collision), \
        "genuine supersession must still be flagged on the keyword path"


def test_c2_08_keyword_path_does_not_flag_the_superseding_doc(searchable_collision):
    assert "teamA/status-new.md" not in _kw_flagged(searchable_collision), \
        "the superseding doc itself is live, never flagged"


def test_c2_08_pathform_edge_keyword_path_is_exact(searchable_pathform):
    flagged = _kw_flagged(searchable_pathform)
    assert "teamB/status.md" not in flagged, "path-form edge must not sweep teamB on the keyword path"
    assert "teamA/status.md" in flagged, "path-form edge must still retire teamA/status.md on the keyword path"
