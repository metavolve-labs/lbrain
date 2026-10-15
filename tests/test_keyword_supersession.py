"""Fortify step 2b (addendum 2026-10-15): keyword-path demotion and successor pull-in.

Measured on S1 before this change: with every retirement route marking correctly, the keyword
path still served the retired record at rank 1 above its successor (P1, P2, P5), because it
ranks by FTS relevance alone and the successor often shares fewer query terms.
(a) `keyword_supersede_penalty` (1.0 = today) multiplies a flagged hit's score and re-ranks.
(b) `successor_pull_in` (off = today) serves the successor's best chunk beside a served retired
    hit, ONLY from an edge or a NAMED successor (CSO gate 2b(b)), never from a bare status or
    path marker, at most one per retired record; a successor already below the cut is lifted.
Each behaviour is paired with its opposite; the defaults arm pins today's behaviour.
"""
import struct

import pytest

from lbrain import index
from lbrain.config import Config
from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import keyword_only
from lbrain.serve import _header
from lbrain.store import Store

DIM = 16


@pytest.fixture(autouse=True)
def _reset():
    index.set_banner_retirement(True)          # the banner route supplies the named successor
    index.set_keyword_supersession(1.0, False)
    yield
    index.set_banner_retirement(False)
    index.set_keyword_supersession(1.0, False)


_DOCS = {
    # named successor that shares NO query term (pull-in must use its opening chunk)
    "a/old.md": "> SUPERSEDED by new.md\n\n# ZQ-ORCA cadence\n\nZQ-ORCA cadence ZQ-ORCA cadence is weekly.\n",
    "a/new.md": "# Revised schedule\n\nNow monthly, decided at the review.\n",
    # status-only retirement: no name, so nothing may be pulled
    "s/dead.md": "---\nstatus: retired\n---\n# ZQ-HERON ledger\n\nZQ-HERON ledger ZQ-HERON ledger total 40.\n",
    "s/heron-live.md": "# Current bird tally\n\nThe count is 52.\n",   # shares no query term
    # edge route: the successor declares Supersedes, the retired record names nothing
    "e/old2.md": "# ZQ-WREN rate\n\nZQ-WREN rate ZQ-WREN rate is 3.\n",
    "e/new2.md": "# Corrected figure\n\n**Supersedes:** [[old2]]\n\nIt is 5.\n",   # shares no query term
    # fillers that out-match the successor, to push it below a small cut
    "f/f1.md": "# ZQ-ORCA notes one\n\nZQ-ORCA cadence notes one.\n",
    "f/f2.md": "# ZQ-ORCA notes two\n\nZQ-ORCA cadence notes two.\n",
}


class FakeEmbedder:
    def __init__(self, dim=DIM): self.dim = dim
    def embed(self, texts, batch_size=64): return [struct.pack(f"{self.dim}f", *([1.0] + [0.0] * (self.dim - 1))) for _ in texts]
    def embed_one(self, text): return self.embed([text])[0]
    def close(self): pass


def _store(tmp_path, docs=_DOCS):
    root = tmp_path / "corpus"
    for rel, text in docs.items():
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8")
    store = Store(tmp_path / "brain.db", embedding_dim=DIM)
    emb = FakeEmbedder()
    for rel in docs:
        doc = parse(root / rel, repo_root=root)
        store.upsert_doc(doc)
        chunks = chunk_doc(doc)
        ids = store.insert_chunks(chunks)
        store.write_embeddings(ids, emb.embed([c.text for c in chunks]))
        store.replace_wikilinks(doc)
        store.replace_supersessions(doc)
    store.db.commit()
    return store


def _paths(hits): return [h.rel_path for h in hits]


def test_defaults_are_todays_behaviour(tmp_path):
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-ORCA cadence", k=10)
    assert _paths(hits)[0] == "a/old.md", "today: the retired record ranks first on relevance"
    assert hits[0].boosts.get("retired") == 1.0, "flag only"
    assert "a/new.md" not in _paths(hits), "today: no pull-in"


def test_penalty_demotes_and_reranks(tmp_path):
    index.set_keyword_supersession(0.25, False)
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-ORCA cadence", k=10)
    old = next(h for h in hits if h.rel_path == "a/old.md")
    assert old.boosts.get("retired") == 0.25
    assert _paths(hits).index("a/old.md") > 0, "a live record now outranks the retired one"


def test_pull_in_named_successor_with_no_query_term(tmp_path):
    index.set_keyword_supersession(0.25, True)
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-ORCA cadence", k=10)
    p = _paths(hits)
    assert "a/new.md" in p, "the named successor is served beside its retired record"
    assert p.index("a/new.md") < p.index("a/old.md")
    new = next(h for h in hits if h.rel_path == "a/new.md")
    assert new.boosts.get("successor_pulled_in") == 1.0 and new.pulled_in_for == "a/old.md"
    assert "SUCCESSOR of a/old.md (pulled in)" in _header(1, new, "ADMISSIBLE")


def test_pull_in_lifts_a_successor_ranked_below_the_cut(tmp_path):
    docs = dict(_DOCS)
    docs["a/new.md"] = "# Revised schedule\n\nZQ-ORCA now monthly.\n"   # matches weakly: below a cut of 2
    index.set_keyword_supersession(1.0, False)
    store = _store(tmp_path, docs)
    assert "a/new.md" not in _paths(keyword_only(store, "ZQ-ORCA cadence", k=2)), "precondition: below the cut"
    index.set_keyword_supersession(0.25, True)
    hits = keyword_only(store, "ZQ-ORCA cadence", k=2)
    assert "a/new.md" in _paths(hits), "lifted into the served set"
    assert _paths(hits).count("a/new.md") == 1, "lifted, never duplicated"


def test_status_only_retirement_pulls_nothing(tmp_path):
    index.set_keyword_supersession(0.25, True)
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-HERON ledger", k=10)
    assert "s/heron-live.md" not in _paths(hits), "a bare status names no successor: nothing is pulled"
    assert "s/dead.md" in _paths(hits), "precondition: the status-retired record is served"
    assert not any(h.pulled_in_for == "s/dead.md" for h in hits), "and nothing is pulled in on its behalf"


def test_edge_with_one_source_pulls_its_source(tmp_path):
    index.set_keyword_supersession(0.25, True)
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-WREN rate", k=10)
    assert "e/new2.md" in _paths(hits)
    assert next(h for h in hits if h.rel_path == "e/new2.md").pulled_in_for == "e/old2.md"


def test_current_only_is_untouched(tmp_path):
    index.set_keyword_supersession(0.25, True)
    store = _store(tmp_path)
    hits = keyword_only(store, "ZQ-ORCA cadence", k=10, current_only=True)
    assert "a/old.md" not in _paths(hits)
    assert not any("successor_pulled_in" in h.boosts for h in hits), "current_only excludes; it does not pull"


@pytest.mark.parametrize("bad", [0, -0.5, 1.5, "x", None])
def test_out_of_range_penalty_warns_and_falls_back(bad):
    with pytest.warns(UserWarning, match="keyword_supersede_penalty"):
        index.set_keyword_supersession(bad, False)
    assert index.keyword_supersession() == (1.0, False)


def test_config_round_trip_arms_both(tmp_path, monkeypatch):
    from lbrain import config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.toml")
    cfgmod.Config(keyword_supersede_penalty=0.25, successor_pull_in=True).write()
    text = (tmp_path / "config.toml").read_text()
    assert "keyword_supersede_penalty = 0.25" in text and "successor_pull_in = true" in text
    cfgmod.Config.load()
    assert index.keyword_supersession() == (0.25, True)
    cfgmod.Config().write()
    cfgmod.Config.load()
    assert index.keyword_supersession() == (1.0, False)
