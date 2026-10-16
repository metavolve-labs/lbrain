"""Gate v2 source collapse (2026-10-16): the candidate is judged against the PRIOR EPOCH per source root.

CCO-REFRESH-20261016T111454: a wake build produced a candidate with 0 docs while every source root was present
and populated; the deletion manifest passed (the roots existed) and only `chunks < 1` caught it, so a PARTIAL
collapse would have published. CCO review 122655Z: an intended delist, a first build and a confirmed removal
must NOT fail. Every test asserts behaviour (a failure naming the root, or none), never full message wording.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lbrain import epoch_build, index

from _coldembed import seed_brain

DIM = 384


@pytest.fixture(autouse=True)
def _reset_markers():
    index.set_exclude_markers(())
    yield
    index.set_exclude_markers(())


def _home(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    src = tmp_path / "src"; src.mkdir()
    for i in range(2):
        (src / f"keep{i}.md").write_text(f"# Keep {i}\n\nA kept record number {i}.\n")
    (home / "config.toml").write_text(
        'embedding_provider = "local"\nembedding_model = "BAAI/bge-small-en-v1.5"\n'
        f'embedding_dim = {DIM}\ndb_path = "{home / "brain.db"}"\nsources = ["{src}"]\n')
    seed_brain(home, DIM)                      # a real 2-doc candidate db
    return home, str(src)


def _collapse(failures, src):
    return [f for f in failures if f.startswith("source-collapse:") and src in f]


def _validate(home, src, prior, confirmed=frozenset()):
    return epoch_build.validate_candidate(home / "brain.db", embedding_dim=DIM, sources=[src],
                                         prior_inv={src: prior}, confirmed_removed=set(confirmed))


def test_partial_collapse_is_refused_naming_the_root(tmp_path):
    """Prior epoch held 10 docs under the root; the candidate kept 2. Roots exist on disk, so the old gate passed."""
    home, src = _home(tmp_path)
    prior = {f"keep{i}.md": "h" for i in range(2)} | {f"gone{i}.md": "h" for i in range(8)}
    assert _collapse(_validate(home, src, prior), src)


def test_intended_delist_is_not_a_collapse(tmp_path):
    """The 8 missing docs sit under an exclude marker: an intended delist, expected 2, kept 2."""
    home, src = _home(tmp_path)
    index.set_exclude_markers(["/claimed/"])
    prior = {f"keep{i}.md": "h" for i in range(2)} | {f"claimed/mail{i}.md": "h" for i in range(8)}
    assert not _collapse(_validate(home, src, prior), src)


def test_first_build_has_no_baseline(tmp_path):
    home, src = _home(tmp_path)
    assert not _collapse(_validate(home, src, {}), src)


def test_small_natural_drop_passes(tmp_path):
    """3 expected, 2 kept: 67% kept is above the 50% floor (a deleted note is ordinary work)."""
    home, src = _home(tmp_path)
    prior = {f"keep{i}.md": "h" for i in range(2)} | {"deleted.md": "h"}
    assert not _collapse(_validate(home, src, prior), src)


def test_confirmed_removal_overrides(tmp_path):
    home, src = _home(tmp_path)
    prior = {f"keep{i}.md": "h" for i in range(2)} | {f"gone{i}.md": "h" for i in range(8)}
    assert not _collapse(_validate(home, src, prior, confirmed={src}), src)


def test_floor_is_the_50_percent_line(tmp_path):
    """Exactly at the floor (2 of 4) passes; one more expected doc (2 of 5) refuses."""
    home, src = _home(tmp_path)
    four = {f"keep{i}.md": "h" for i in range(2)} | {f"gone{i}.md": "h" for i in range(2)}
    five = four | {"gone2.md": "h"}
    assert not _collapse(_validate(home, src, four), src)
    assert _collapse(_validate(home, src, five), src)
