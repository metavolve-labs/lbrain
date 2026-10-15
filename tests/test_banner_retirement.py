"""Fortify step 2 (A13, CSO plan 2026-10-15): prose retirement banners as a retirement route.

Measured on the CTO brain before this change: the superseded `FRAMEWORK-...-v0` served at rank 1
on BOTH paths above its named successor `v02`, whose name is on v0's first line. No route read
a banner. `banner_retirement` (default false = today) reads a line-initial SUPERSEDED / RETIRED /
ARCHIVED banner in the first 15 lines; "IN PART" and its kin flag the record and never penalise
it (TOUCH3: part of it still binds). Each behaviour is paired with its opposite, so the tests
fail in both directions, and the flag-off arm pins today's behaviour.
"""
import struct

import pytest

from lbrain import index
from lbrain.config import Config
from lbrain.index import chunk as chunk_doc
from lbrain.index import parse, parse_retirement_banner
from lbrain.search import _resolve_self_retired_paths, keyword_only, search
from lbrain.serve import _header
from lbrain.store import Store

DIM = 32


@pytest.fixture(autouse=True)
def _reset_flag():
    index.set_banner_retirement(False)
    yield
    index.set_banner_retirement(False)


# ---- parser: shapes taken from the live corpus (S1 set and the 12 fires labelled 2026-10-15) ----

@pytest.mark.parametrize("text,want", [
    ("> SUPERSEDED for current build direction by FRAMEWORK-x-v02-2026-10-13.md. Historical draft retained below.\n# v0",
     ("full", "FRAMEWORK-x-v02-2026-10-13.md")),
    ("# Report\n\n> ⚠️ **SUPERSEDED by v3.1** (`REPORT-v3.1-FINAL.md`, 23:59Z): this\n> version was withdrawn.\n",
     ("full", "REPORT-v3.1-FINAL.md")),
    ("Superseded 2026-05-14 — see [[feedback_lbrain_usage]] for current guidance.", ("full", "feedback_lbrain_usage")),
    ("# SUPERSEDED — do not read as current\n\nbody", ("full", "")),
    ("> SUPERSEDED by proximity_proof_v0.2.tex — numbers herein may be stale.", ("full", "proximity_proof_v0.2.tex")),
    ("# Touch 3\n\n> **⚠️ SUPERSEDED same day by Tad's video-led gameplan —\n> `GAMEPLAN-VIDEO.md` is the plan of record.** "
     "The disclosure envelope\n> and live-verified state rows below still bind; the letter-led sequencing does not.",
     ("part", "GAMEPLAN-VIDEO.md")),
    ("> ### ⚠️ SUPERSEDED IN PART — read `ROADMAP-2026-07-28.md` first", ("part", "ROADMAP-2026-07-28.md")),
    ("> ⛔ **SUPERSEDED IN ITS CENTRAL CLAIM, 2026-09-13T01:38Z — see\n> `AMENDMENT-1.md`", ("part", "AMENDMENT-1.md")),
])
def test_banner_fires(text, want):
    assert parse_retirement_banner(text) == want


@pytest.mark.parametrize("text", [
    "# The retired-claims registry is read at DEPLOY time, never at SERVE time\n\nbody",   # a doc ABOUT retirement
    "Line one of a paragraph that is\nsuperseded by this paragraph. Source pins are recorded.",  # wrapped prose
    "- SUPERSEDED edges are resolved per path\n",                                         # a list item is content
    "Superseded edges are rare in this corpus.",                                         # mixed case, no successor
    "\n".join(["line"] * 15 + ["> SUPERSEDED by later.md"]),                             # past the first 15 lines
    "",
])
def test_banner_does_not_fire(text):
    assert parse_retirement_banner(text) is None


def test_flag_parsing_is_explicit_true_only():
    for v in (True, "true", "yes", "1"):
        index.set_banner_retirement(v)
        assert index.banner_retirement_enabled()
    for v in (False, None, "", "no", "flase", 0):
        index.set_banner_retirement(v)
        assert not index.banner_retirement_enabled()


# ---- retrieval: both paths, flag off (today) and on ----

_DOCS = {
    "f/frame-v0.md": ("> SUPERSEDED for current build direction by frame-v02.md. Historical draft retained below.\n\n"
                      "# ZK-FRAMEWORK build direction v0\n\nZK-FRAMEWORK build direction is two personas, one seat.\n"),
    "f/frame-v02.md": "# ZK-FRAMEWORK build direction v02\n\nZK-FRAMEWORK build direction is one identity, two seats.\n",
    "t/touch3.md": ("# ZK-TOUCH plan\n\n> **SUPERSEDED same day by `touch3-video.md`.** The envelope rows below still bind.\n\n"
                    "ZK-TOUCH counsel initials are CMPR. ZK-FRAMEWORK unrelated.\n"),
    "t/touch3-video.md": "# ZK-TOUCH video plan\n\nZK-TOUCH video-led sequencing.\n",
    "a/about-retirement.md": ("# The retired-claims registry is wired everywhere except serving\n\n"
                              "ZK-FRAMEWORK retired registry wiring, a live document about retirement.\n"),
}


class FakeEmbedder:
    def __init__(self, dim=DIM): self.dim = dim
    def _vec(self, text):
        acc = [0.0] * self.dim
        for tok in text.lower().split():
            acc[hash(tok) % self.dim] += 1.0
        n = sum(v * v for v in acc) ** 0.5 or 1.0
        return struct.pack(f"{self.dim}f", *[v / n for v in acc])
    def embed(self, texts, batch_size=64): return [self._vec(t) for t in texts]
    def embed_one(self, text): return self._vec(text)
    def close(self): pass


def _brain(tmp_path):
    root = tmp_path / "corpus"
    for rel, text in _DOCS.items():
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8")
    cfg = Config(embedding_provider="local", embedding_dim=DIM)
    cfg.rrf_k = 60; cfg.supersede_aware = True; cfg.supersede_penalty = 0.25
    cfg.db_path = tmp_path / "brain.db"
    store = Store(cfg.db_path, embedding_dim=DIM)
    emb = FakeEmbedder()
    for rel in _DOCS:
        doc = parse(root / rel, repo_root=root)
        store.upsert_doc(doc)
        chunks = chunk_doc(doc)
        ids = store.insert_chunks(chunks)
        store.write_embeddings(ids, emb.embed([c.text for c in chunks]))
        store.replace_wikilinks(doc)
        store.replace_supersessions(doc)
    store.db.commit()
    return cfg, store, emb


def _by(hits, rel): return next((h for h in hits if h.rel_path == rel), None)


def test_flag_off_is_todays_behaviour(tmp_path):
    cfg, store, emb = _brain(tmp_path)
    assert _resolve_self_retired_paths(store) == set()
    for hits in (keyword_only(store, "ZK-FRAMEWORK build direction", k=10),
                 search(cfg, store, emb, "What is the ZK-FRAMEWORK build direction?", k=10)):
        v0 = _by(hits, "f/frame-v0.md")
        assert v0 is not None and not ({"retired", "superseded", "superseded_in_part"} & set(v0.boosts))


def test_flag_on_full_banner_retires_and_names_successor_on_both_paths(tmp_path):
    index.set_banner_retirement(True)
    cfg, store, emb = _brain(tmp_path)
    assert _resolve_self_retired_paths(store) == {"f/frame-v0.md"}, "only the full banner retires"
    kw = keyword_only(store, "ZK-FRAMEWORK build direction", k=10)
    v0 = _by(kw, "f/frame-v0.md")
    assert v0 and v0.boosts.get("retired") == 1.0, "keyword path: flag only"
    assert "RETIRED → corrected by: f/frame-v02.md" in _header(1, v0, "ADMISSIBLE")
    ranked = search(cfg, store, emb, "What is the ZK-FRAMEWORK build direction?", k=10)
    paths = [h.rel_path for h in ranked]
    assert _by(ranked, "f/frame-v0.md").boosts.get("retired") == 0.25
    assert paths.index("f/frame-v02.md") < paths.index("f/frame-v0.md"), "successor above the retired doc"


def test_flag_on_partial_banner_flags_never_penalises(tmp_path):
    index.set_banner_retirement(True)
    cfg, store, emb = _brain(tmp_path)
    assert "t/touch3.md" not in _resolve_self_retired_paths(store), "IN PART must not retire in full"
    for hits in (keyword_only(store, "ZK-TOUCH counsel initials", k=10),
                 search(cfg, store, emb, "ZK-TOUCH counsel initials", k=10)):
        t3 = _by(hits, "t/touch3.md")
        assert t3 is not None
        assert t3.boosts.get("superseded_in_part") == 1.0
        assert "retired" not in t3.boosts and "superseded" not in t3.boosts
        assert "SUPERSEDED IN PART → see: t/touch3-video.md" in _header(1, t3, "ADMISSIBLE")


def test_flag_on_does_not_touch_a_live_doc_about_retirement(tmp_path):
    index.set_banner_retirement(True)
    cfg, store, emb = _brain(tmp_path)
    hits = keyword_only(store, "ZK-FRAMEWORK retired registry wiring", k=10)
    doc = _by(hits, "a/about-retirement.md")
    assert doc is not None and not ({"retired", "superseded", "superseded_in_part"} & set(doc.boosts))


def test_config_write_then_load_arms_the_flag(tmp_path, monkeypatch):
    from lbrain import config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.toml")
    cfgmod.Config(banner_retirement=True).write()
    assert "banner_retirement = true" in (tmp_path / "config.toml").read_text()
    assert cfgmod.Config.load().banner_retirement is True
    assert index.banner_retirement_enabled()
    cfgmod.Config(banner_retirement=False).write()
    cfgmod.Config.load()
    assert not index.banner_retirement_enabled()
