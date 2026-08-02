"""A-408 — the authoring framework must reach users, not sit in a private repo.

Ranking quality is bounded by corpus quality: a perfect retriever over a corpus of
confident guesses returns confident guesses, faster. Shipping the engine without
its authoring contract ships half a product. 36 KB of framework docs previously
lived in docs/, were packaged by nothing, and were linked from a README pointing
at a private repository — reachable by no user at all.
"""
import zipfile
from pathlib import Path

import pytest

from lbrain.framework import DOCS, path, read


def test_every_advertised_doc_exists():
    for name in DOCS:
        assert path(name).exists(), name
        assert len(read(name)) > 500, f"{name} is a stub"


def test_unknown_doc_names_the_available_ones():
    with pytest.raises(FileNotFoundError) as e:
        path("NOPE")
    assert "AUTHORING_DISCIPLINE" in str(e.value)


def test_discipline_doc_states_what_the_tool_cannot_do():
    """The honest framing is load-bearing: without it this reads as a blog post.

    The tool enforces dating, staleness, supersession and attribution. It cannot
    tell whether a claim is TRUE. Saying so is what makes the rest credible.
    """
    body = read("AUTHORING_DISCIPLINE")
    assert "cannot tell whether what you wrote is true" in body.lower()


def test_framework_contains_nothing_org_specific():
    """It ships to strangers. A leaked internal identifier is a disclosure bug."""
    import re
    bad = re.compile(
        r"metavolve|golden.?codex|artiswa|aeternum|atmtad|curator@|BPLL|"
        r"0x[0-9a-fA-F]{8}|/mnt/[a-z]/|C:\\\\Users",
        re.I,
    )
    for name in DOCS:
        hits = bad.findall(read(name))
        assert not hits, f"{name} leaks {hits}"


def test_shipped_source_contains_no_internal_corpus_identifiers():
    """The guard above only ever scanned the framework docs — and every real leak
    was somewhere else.

    2026-08-03: six internal identifiers were found in shipped *code and docs*
    (`consolidate.py`, `search.py`, `index.py`, `disclosure.py`, and a design doc
    the README links publicly), naming private lair directories and the founder
    by name. The existing guard passed the whole time because it looked only at
    `lbrain/framework/*.md`.

    So this scans everything that actually ships. Cleaning the six sites fixes an
    instance; this fixes the class — the same distinction A-404 turned on, where
    the reported path-split bug had four more copies nobody looked for.
    """
    import re
    root = Path(__file__).resolve().parents[1]
    bad = re.compile(
        r"P\d-[A-Z]{4,}"          # plate folders: P3-NEURAINETIC-BRAIN
        r"|NEURAINETIC"
        r"|000-(?:OPERATING|IDENTITY)-DOCTRINE"
        r"|lairs/\d{3}-PRIORITY"  # concrete internal lair paths
        r"|codex-curator"
        r"|\bTad\b"
        r"|atmtad"
        r"|/mnt/[a-z]/Users",
    )
    # onboard.py legitimately GENERATES a lair layout for the user's own corpus;
    # test files deliberately contain these strings as fixtures and as this guard.
    skip = {"onboard.py"}
    offenders = {}
    for path in [*(root / "lbrain").rglob("*.py"), *(root / "docs").rglob("*.md")]:
        if path.name in skip or "framework" in path.parts:
            continue
        hits = bad.findall(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            offenders[str(path.relative_to(root))] = sorted(set(hits))
    assert not offenders, f"internal identifiers in shipped source: {offenders}"


def test_docs_are_present_in_a_built_wheel():
    """The bug was never 'the files are missing' — it was 'nothing packages them'."""
    dists = sorted(Path(__file__).resolve().parents[1].glob("dist/*.whl"))
    if not dists:
        pytest.skip("no wheel built")
    names = zipfile.ZipFile(dists[-1]).namelist()
    shipped = [n for n in names if n.startswith("lbrain/framework/") and n.endswith(".md")]
    assert len(shipped) == len(DOCS), f"wheel ships {shipped}, expected {len(DOCS)} docs"
