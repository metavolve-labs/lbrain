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


def test_selftest_catches_removed_fencing(monkeypatch):
    # CSO review of #13, finding 1 (security-relevant): the old `"│ " in out`
    # check passed on the warning banner's description of the fence, so it stayed
    # green with fencing entirely removed. Reproduce the CSO's mutation — strip the
    # fence prefix from body lines — and require selftest to FAIL.
    import lbrain.serve as serve

    orig = serve.fence_block
    monkeypatch.setattr(serve, "fence_block", lambda text: orig(text).replace("│ ", "| "))
    assert run_selftest() is False


def test_selftest_catches_wrong_retrieval_rank(monkeypatch):
    # CSO review of #13, finding 3: presence-not-rank. The retrieval invariant
    # must fail when the target is retrieved but NOT ranked first. Deterministic
    # mutation (independent of corpus size): prepend a decoy so the relevant doc
    # is present but no longer at rank 1.
    import lbrain.search as search
    from lbrain.search import Hit

    orig = search.keyword_only

    def demoted(*a, **k):
        decoy = Hit(rel_path="decoy.md", chunk_idx=0, text="unrelated", title="Decoy", score=99.0)
        return [decoy, *orig(*a, **k)]

    monkeypatch.setattr(search, "keyword_only", demoted)
    assert run_selftest() is False
