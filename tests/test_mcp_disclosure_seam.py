"""The MCP↔disclosure seam — a control enforced on SOME paths is not a control.

Found 2026-08-01 by adversarial audit and reproduced end to end. `lair_check_action`
was the one retrieval path that never received the disclosure envelope. With
`allowed_path_prefixes = ["public/"]`, `lair_query` correctly withheld a private
record while `lair_check_action` returned its VERBATIM text — and returned it with
no blinding notice, because there was no `withheld` object to render one from.

It is the worst tool to have this hole: its own docstring tells a model to call it
immediately BEFORE something irreversible.

Root cause was coverage, not logic. The audit mutated `lair_query` to drop the
envelope entirely and the suite still passed 344/344 — engine-level tests bite, the
TOOL boundary was unpinned. These tests pin the boundary.
"""
from __future__ import annotations

import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.store import Store

from test_ranking import DIM, FakeEmbedder, _cfg

CANARY = "CANARY-PRIVATE-NEVER-LEAK"

DOCS = {
    "public/feedback-deploy.md": (
        "---\ntype: feedback\n---\n# Deploy rule\n\n"
        "never deploy pricing changes on a friday afternoon\n"
    ),
    "private/feedback-margin.md": (
        "---\ntype: feedback\n---\n# Margin rule\n\n"
        f"{CANARY} never deploy pricing changes below the 42 percent margin floor\n"
    ),
}


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    """A brain with a STANDING scope: only `public/` is in view.

    Standing, not per-request — `permitted()` documents itself as applying "in
    every mode, including full", which is exactly the claim the bypassed path
    falsified.
    """
    root = tmp_path / "corpus"
    for rel, text in DOCS.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    cfg = _cfg()
    cfg.db_path = tmp_path / "brain.db"
    cfg.allowed_path_prefixes = ["public/"]
    store = Store(cfg.db_path, embedding_dim=DIM)
    emb = FakeEmbedder()

    for rel in DOCS:
        doc = parse(root / rel, repo_root=root)
        store.upsert_doc(doc)
        chunks = chunk_doc(doc)
        ids = store.insert_chunks(chunks)
        store.write_embeddings(ids, emb.embed([c.text for c in chunks]))
        store.replace_wikilinks(doc)
        store.replace_supersessions(doc)
    store.db.commit()

    import lbrain.mcp_server as m

    monkeypatch.setattr(m, "Config", type("C", (), {"load": staticmethod(lambda: cfg)}))
    monkeypatch.setattr(m, "make_embedder", lambda _c: emb)
    # Deliberately NOT monkeypatching Store: each tool must open its own handle
    # against the committed DB. Sharing one object made the first tool's close()
    # kill the second — a fixture artifact that would have looked like a bug.
    store.db.commit()
    store.close()
    yield m


def _act(m):
    return m.lair_check_action("deploy pricing changes tomorrow afternoon")


def test_check_action_does_not_leak_out_of_scope_text(scoped):
    """The finding, pinned. This is the assertion that was missing."""
    out = _act(scoped)
    assert CANARY not in out, f"out-of-scope record leaked verbatim:\n{out}"


def test_check_action_still_returns_IN_scope_warnings(scoped):
    """Negative control — without it, an empty return would pass the test above.

    A scoping test that cannot tell "correctly filtered" from "returned nothing"
    proves nothing, which is the defect class that let this ship in the first place.
    """
    out = _act(scoped)
    assert "friday" in out.lower(), f"in-scope warning was lost too:\n{out}"


def test_check_action_discloses_that_it_blinded(scoped):
    """Silently handing an agent a thinned corpus is subtler and worse than refusing."""
    assert "WITHHELD" in _act(scoped).upper()


def test_query_and_check_action_agree_on_scope(scoped):
    """The invariant the seam violated: two tools, one corpus, one scope."""
    assert (CANARY in scoped.lair_query("deploy pricing changes")) is False
    assert (CANARY in _act(scoped)) is False


def test_check_action_on_an_empty_brain_is_not_a_green_light(tmp_path, monkeypatch):
    """"No conflicts" from an UNPROVISIONED brain is a confident go-ahead on an
    irreversible action — A-425 at its most dangerous, and the one tool missed when
    the other four were swept."""
    import importlib

    home = tmp_path / "empty"
    home.mkdir()
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config
    import lbrain.mcp_server

    importlib.reload(lbrain.config)
    m = importlib.reload(lbrain.mcp_server)
    try:
        out = m.lair_check_action("delete the production database")
        assert "UNPROVISIONED BRAIN" in out
        assert out.index("UNPROVISIONED") < out.index("No conflicts")
    finally:
        # Restore module globals for whatever runs next. reload() mutates them
        # process-wide, and leaving them pointed at a tmp_path is exactly the
        # order-dependent pollution the suite randomizes to catch.
        monkeypatch.delenv("LBRAIN_HOME", raising=False)
        importlib.reload(lbrain.config)
        importlib.reload(lbrain.mcp_server)
