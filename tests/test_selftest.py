"""`lbrain selftest` must PASS on a working build and FAIL on a broken one.

The second test is the point: a self-check that can only ever pass is worthless
(that was the A-442 doctor failure — a guard that gave a broken index a clean
bill of health). Breaking the serve path here must turn selftest red.
"""
from lbrain.selftest import run_selftest


def test_selftest_passes_on_this_build():
    assert run_selftest() is True


def test_selftest_catches_a_broken_serve_path(monkeypatch):
    # Break render_response so the render-based invariants can no longer find
    # their markers. selftest must report failure — otherwise it is not a gate.
    import lbrain.serve as serve

    monkeypatch.setattr(serve, "render_response", lambda *a, **k: "")
    assert run_selftest() is False


def test_selftest_catches_broken_retrieval(monkeypatch):
    # Break the retrieval path (no embedder needed — this is the FTS path a fresh
    # install actually uses). selftest must catch it too.
    import lbrain.search as search

    monkeypatch.setattr(search, "keyword_only", lambda *a, **k: [])
    assert run_selftest() is False
