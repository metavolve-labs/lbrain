"""RSI-PRELIM-1 engine landmine AX-06/L1: basename-slug supersession collision.

teamA/status.md and teamB/status.md share the basename slug 'status'. When
teamA/status-new.md supersedes teamA/status.md, teamB/status.md must NOT be
penalized (path-qualified identity). teamA/status.md MUST still be de-ranked.
"""
import tempfile
from pathlib import Path

import pytest

from lbrain.config import Config
from lbrain.store import Store
from lbrain.index import parse


@pytest.fixture()
def store_with_collision(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    corpus = tmp_path / "corpus"
    for rel, txt in [
        ("teamA/status.md", "# A status\n\nteamA current status content alpha bravo.\n"),
        ("teamA/status-new.md", "# A status new\n\n**Supersedes:** [[status]]\n\nteamA new status alpha bravo.\n"),
        ("teamB/status.md", "# B status\n\nteamB unrelated status content alpha bravo.\n"),
    ]:
        p = corpus / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt)
    st = Store(home / "brain.db")
    for rel in ("teamA/status.md", "teamA/status-new.md", "teamB/status.md"):
        doc = parse(corpus / rel, repo_root=corpus)
        st.upsert_doc(doc)
        st.replace_supersessions(doc)
    st.db.commit()
    return st


def _superseded_paths(st):
    from lbrain.search import _resolve_superseded_paths
    return _resolve_superseded_paths(st)


def test_ax06_teamB_not_swept_by_teamA_supersession(store_with_collision):
    resolved = _superseded_paths(store_with_collision)
    assert "teamB/status.md" not in resolved, "unrelated same-basename doc must NOT be buried"


def test_ax06_teamA_original_still_superseded(store_with_collision):   # NO-REGRESSION
    resolved = _superseded_paths(store_with_collision)
    assert "teamA/status.md" in resolved, "the genuinely-superseded original must still be de-ranked"
