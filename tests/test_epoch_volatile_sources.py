"""volatile_sources (2026-11-17): a drainable root (an inbox) may fall toward zero without reading as a collapse or a
hollow root, under the CSO's conditions: the exemption cannot cover every source, a volatile root may not hold more
than VOLATILE_MAX_SHARE of the prior epoch's documents, and the other roots stay guarded exactly as before.

Found on the CCO brain: its inbox root went 13 -> 0 docs as mail was claimed and gate v2 refused every candidate
for ten hours.
"""
from __future__ import annotations

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
    inbox = tmp_path / "inbox"; inbox.mkdir()          # exists, enumerates empty: the drained-inbox shape
    (home / "config.toml").write_text(
        'embedding_provider = "local"\nembedding_model = "BAAI/bge-small-en-v1.5"\n'
        f'embedding_dim = {DIM}\ndb_path = "{home / "brain.db"}"\nsources = ["{src}", "{inbox}"]\n')
    seed_brain(home, DIM)
    return home, str(src), str(inbox)


def _abs(root, *names):
    return {f"{root}/{n}": "h" for n in names}


def _validate(home, src, inbox, prior_src, prior_inbox, volatile=frozenset()):
    prior_inv = {src: prior_src, inbox: prior_inbox}
    return epoch_build.validate_candidate(home / "brain.db", embedding_dim=DIM, sources=[src, inbox],
                                         prior_inv=prior_inv, confirmed_removed=set(),
                                         prior_abs={k: list(v) for k, v in prior_inv.items()},
                                         volatile=volatile)


def _about(failures, root):
    return [f for f in failures if root in f]


def test_drained_inbox_refuses_without_the_exemption(tmp_path):
    home, src, inbox = _home(tmp_path)
    fails = _validate(home, src, inbox, _abs(src, "keep0.md", "keep1.md"), _abs(inbox, *[f"m{i}.md" for i in range(13)]))
    assert _about(fails, inbox), fails


def test_drained_inbox_passes_when_volatile_and_other_roots_stay_guarded(tmp_path):
    home, src, inbox = _home(tmp_path)
    prior_inbox = _abs(inbox, *[f"m{i}.md" for i in range(13)])
    # the inbox is exempt: 13 inbox docs against 302 under the other root (4.1%, under the cap); the 300 "stay"
    # docs sit under an exclude marker so they are an intended delist for the collapse check, not a loss
    index.set_exclude_markers(["/stay"])
    prior_src = _abs(src, "keep0.md", "keep1.md", *[f"stay{i}.md" for i in range(300)])
    fails = _validate(home, src, inbox, prior_src, prior_inbox, volatile={inbox})
    assert not _about(fails, inbox), fails
    # a real collapse on the other root is still refused with the inbox exempt (8 gone docs, not excluded)
    prior_src_big = _abs(src, "keep0.md", "keep1.md", *[f"stay{i}.md" for i in range(300)], *[f"gone{i}.md" for i in range(8)])
    fails = _validate(home, src, inbox, prior_src_big, prior_inbox, volatile={inbox})
    assert any(f.startswith("source-collapse:") and src in f for f in fails), fails


def test_every_source_volatile_is_refused(tmp_path):
    home, src, inbox = _home(tmp_path)
    fails = _validate(home, src, inbox, _abs(src, "keep0.md", "keep1.md"), {}, volatile={src, inbox})
    assert any(f.startswith("volatile_sources: every configured source") for f in fails), fails


def test_volatile_root_above_the_share_cap_is_refused(tmp_path):
    home, src, inbox = _home(tmp_path)
    # the "inbox" held 13 of 15 prior docs: far above the cap; such an exemption could hide a real collapse
    fails = _validate(home, src, inbox, _abs(src, "keep0.md", "keep1.md"), _abs(inbox, *[f"m{i}.md" for i in range(13)]),
                      volatile={inbox})
    assert any(f.startswith("volatile_sources:") and "share" in f for f in fails), fails


def test_volatile_root_under_the_cap_passes(tmp_path):
    home, src, inbox = _home(tmp_path)
    prior_src = _abs(src, "keep0.md", "keep1.md", *[f"stay{i}.md" for i in range(60)])  # 62 kept... see below
    # keep the candidate honest: the src root here only HOLDS keep0/keep1 on disk, so widen the prior to many kept
    # docs by excluding the extras under a marker (an intended delist) and leave 1 inbox doc = 1/63 < 5%.
    index.set_exclude_markers(["/stay"])
    fails = _validate(home, src, inbox, prior_src, _abs(inbox, "m0.md"), volatile={inbox})
    assert not any(f.startswith("volatile_sources:") for f in fails), fails
    assert not _about(fails, inbox), fails
