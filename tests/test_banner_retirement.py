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


# ---- fence fix (CSO 2026-10-15T0719Z: the parser fired inside a code fence on the live
# retired-claims registry; held-out finding, precision 11/13 across all brains) ----

# The trigger, verbatim: the first 15 lines (the parser's whole window) of the CSO brain's
# `_root--retired-claims--derived.md` first chunk. Truncated to the window so no registry row
# below it is published in this public repo.
_FENCE_TRIGGER = """# Retired claims registry (derived view — source of truth: personas/cso/RETIRED.yml)

> YAML rendered into an indexable .md view; the engine indexes .md only (A-412).

```yaml
# TWO LISTS, BOTH DECLARED (CTO decision 2026-08-10).
# This file is the master for CORPUS and GTM surfaces. The public `lbrain` repo
# keeps its own NEVER list in tests/test_human_facing_register.py for repo
# surfaces, because a public repo's tests cannot read a private corpus file and
# mirroring this one there would publish the disease list. They CAN drift; that
# is accepted and named here so the drift is visible rather than discovered.
#
# RETIRED — claims adjudicated as dead, with their cause of death.
#
# A number appearing in a document is not permission to use it. This file is the"""


def test_fence_trigger_verbatim_returns_none():
    assert "# RETIRED \u2014 claims adjudicated as dead" in _FENCE_TRIGGER, "precondition: the trigger line is present"
    assert parse_retirement_banner(_FENCE_TRIGGER) is None


def test_fence_trigger_fired_before_the_fix_shape():
    """Negative control on the fixture: the same line OUTSIDE a fence still reads as a banner,
    so the None above is the fence skip, not a fixture the parser could never fire on."""
    unfenced = _FENCE_TRIGGER.replace("```yaml\n", "")
    assert parse_retirement_banner(unfenced) is not None


@pytest.mark.parametrize("text,want", [
    ("~~~\n# SUPERSEDED by x.md\n~~~\n", None),                                   # tilde fence
    ("> ```\n> SUPERSEDED by x.md\n> ```\n", None),                                # fence inside a blockquote
    ("```\ncode\n```\n\n> SUPERSEDED by later.md\n", ("full", "later.md")),     # a banner after a closed fence
])
def test_fence_states(text, want):
    assert parse_retirement_banner(text) == want


def test_state_named_marker_file_is_exempt(tmp_path):
    """A `SUPERSEDED.md` notice describes its directory; the notice itself is current
    (CSO blind label 2026-10-15, fire 8)."""
    docs = dict(_DOCS)
    docs["d/SUPERSEDED.md"] = "# SUPERSEDED \u2014 do not read as current\n\nZK-MARKER this directory was replaced.\n"
    index.set_banner_retirement(True)
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
    store.db.commit()
    assert parse_retirement_banner(docs["d/SUPERSEDED.md"]) == ("full", ""), "precondition: the text alone fires"
    got = _resolve_self_retired_paths(store)
    assert "d/SUPERSEDED.md" not in got and "f/frame-v0.md" in got
