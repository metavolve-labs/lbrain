"""A-513 — a chunk must carry the headings it lives UNDER, not just the one it
starts on.

Found live, not by inspection. A real corpus doc titled

    # RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25

split into H2 sections, and the section holding a superseded count served as if
it were live work:

    [1] binds      "Current corpus = 8,871 RFCs numbered 1000-9999 only"   <- stale
    [2] near-miss  "DONE. 9,791 RFCs minted (+15 pilot = 9,806)"           <- correct

The gate admitted the stale figure and rejected the correct one, and it was
right to on the evidence it had: the stale chunk began with
`## Step 1 - COMPLETE the corpus (BLOCKER - do not skip)` and carried nothing
saying the work had finished. The H1 that says EXECUTED + VERIFIED was thrown
away by the splitter.
"""

from pathlib import Path

from lbrain.index import Doc, chunk, _split_on_headers

# The real shape, reduced: an H1 announcing completion, an H2 recording the old
# state, and a sibling H2 whose text reads as an urgent live blocker.
_BODY = """# RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25

**DONE.** 9,791 RFCs minted (+15 pilot = 9,806).

## State at handoff (2026-07-25)

Everything below describes the plan as it stood before the mint ran.

## Step 1 — COMPLETE the corpus (BLOCKER — do not skip)

Current corpus = 8,871 RFCs numbered 1000–9999 only.
"""


def _doc(body: str = _BODY) -> Doc:
    return Doc(
        path=Path("/corpus/rfc-pilot.md"),
        rel_path="rfc-pilot.md",
        title="RFC pilot",
        body=body,
        metadata={},
        doc_hash="h",
        mtime=0.0,
    )


def test_h2_sections_carry_their_h1():
    sections = _split_on_headers(_BODY)
    paths = {t.splitlines()[0]: p for t, p in sections}
    h1 = "# RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25"
    assert paths[h1] == "", "an H1 is the root — it has no ancestors"
    for h2 in ("## State at handoff (2026-07-25)",
               "## Step 1 — COMPLETE the corpus (BLOCKER — do not skip)"):
        assert paths[h2] == "RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25", (
            f"{h2!r} lost the H1 it lives under — this is the A-513 failure"
        )


def test_the_stale_chunk_can_no_longer_pass_as_live_work():
    """The specific regression: whichever chunk holds 8,871 must also carry the
    fact that the mint was EXECUTED + VERIFIED."""
    chunks = chunk(_doc(), max_tokens=40, overlap=8)
    stale = [c for c in chunks if "8,871" in c.text]
    assert stale, "test corpus no longer contains the stale figure"
    for c in stale:
        combined = f"{c.heading_path}\n{c.text}"
        assert "EXECUTED + VERIFIED" in combined, (
            "the superseded count is served with no sign the work is done"
        )
        assert "2026-07-25" in combined, "no date reaches the deep chunk"


def test_flat_docs_are_untouched():
    """No H2-under-H1 nesting → empty path → legacy hash byte-for-byte, so a flat
    corpus does not re-embed on upgrade."""
    body = "# Only a title\n\nsome prose\n"
    for c in chunk(_doc(body), max_tokens=512, overlap=64):
        assert c.heading_path == ""


def test_windowed_continuations_keep_their_own_section():
    """An oversized H2 splits into several chunks; only the first holds the
    heading in its text. The rest must name the section they came from."""
    body = (
        "# Parent title\n\n"
        "## Big section\n\n" + "\n".join(f"- row {i} of the big section" for i in range(400))
    )
    chunks = chunk(_doc(body), max_tokens=64, overlap=8)
    tail = [c for c in chunks if "row 399" in c.text]
    assert tail, "windowing did not produce a continuation chunk"
    for c in tail:
        assert "Big section" in c.heading_path
        assert "Parent title" in c.heading_path


def test_heading_path_changes_the_chunk_hash():
    """Ancestry reaches the embedding and the FTS row, so a chunk that gains it
    is a CHANGED chunk. If the hash did not move, import would short-circuit and
    leave the old vectors in place."""
    from lbrain.index import _make_chunk

    doc = _doc()
    text = "## Step 1 — COMPLETE the corpus\n\nCurrent corpus = 8,871 RFCs."
    bare = _make_chunk(doc, 0, text, 20)
    with_path = _make_chunk(doc, 0, text, 20, "", "RFC full-corpus mint — EXECUTED")
    assert bare.chunk_hash != with_path.chunk_hash, (
        "identical text under different ancestry hashes the same — import would "
        "short-circuit and leave the stale vectors in place"
    )
    # ...and no path at all must reproduce the pre-A-513 hash exactly, or every
    # flat corpus in the wild re-embeds for nothing.
    assert _make_chunk(doc, 0, text, 20, "", "").chunk_hash == bare.chunk_hash
