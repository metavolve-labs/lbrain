"""Outcome gates for the serving path — assert BEHAVIOUR, not mechanism.

This file is the counterpart to test_heading_ancestry.py, and the gate that
would have caught A-441 / PR #9. Those tests all assert the chunker MECHANISM
(a chunk carries its H1); none runs the real chunker into the serve layer and
checks what a query actually RETURNS. So the fix passed every test it had while
the served outcome was unchanged — #9 merged green and the stale figure still
bound at rank 1.

The rule these tests enforce (repo doctrine; exoskeleton ONBOARDING-REQUIREMENTS
req 13): a change to serving, ranking, or chunking is not "done" until its
OUTCOME is verified end-to-end. *"A passing test on a mechanism is not evidence
about behaviour."* Every test here runs the REAL chunk() into the REAL
render_response and asserts what the reader actually sees.

If you change serving/ranking/chunking, add or extend a test HERE, not only a
unit test on the internal you touched.
"""
from pathlib import Path

from lbrain.config import Config
from lbrain.index import Doc, chunk
from lbrain.search import Hit
from lbrain.serve import render_response

# The real corpus shape that produced C1, reduced: an H1 announcing completion,
# a "DONE" line with the current count, and a sibling H2 that records the OLD
# count as an urgent live blocker. One document, one week — the case where no
# date, filename, slug, or heading deterministically picks the winner.
_RFC_SELF_CORRECTING = """# RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25

**DONE.** 9,791 RFCs minted (+15 pilot = 9,806 RFCs now in the corpus).

## State at handoff (2026-07-25)

Everything below describes the plan as it stood before the mint ran.

## Step 1 — COMPLETE the corpus (BLOCKER — do not skip)

Current corpus = 8,871 RFCs numbered in the corpus before the fix.
"""


def _hits_via_real_chunker(body: str, max_tokens: int = 40, overlap: int = 8) -> list[Hit]:
    """Run the REAL chunker and present its chunks as retrieved hits.

    This exercises the chunk->serve path #9 changed and the serve layer that
    failed — no mocking of chunk boundaries or heading ancestry. Feeding all
    chunks as hits is the faithful adversarial case: it asks "when both the
    stale and the current figure are retrieved, what does the reader see?"
    """
    doc = Doc(path=Path("/corpus/rfc-pilot.md"), rel_path="rfc-pilot.md",
              title="RFC full-corpus mint", body=body, metadata={},
              doc_hash="h", mtime=0.0)
    return [
        Hit(rel_path="rfc-pilot.md", chunk_idx=c.chunk_idx, text=c.text,
            title="RFC full-corpus mint", score=1.0 - i * 0.01,
            heading_path=c.heading_path)
        for i, c in enumerate(chunk(doc, max_tokens=max_tokens, overlap=overlap))
    ]


def _cfg() -> Config:
    cfg = Config()
    cfg.core_memory_path = ""   # no core block in tests
    return cfg


def test_c1_stale_figure_is_not_served_as_the_lone_answer():
    """A-441 / PR #9, tested on the OUTCOME. When the retrieved chunks hold both
    the superseded "8,871" and the current "9,806", a quantity query must NOT
    present the stale figure as the sole confident answer. #9's tests all passed
    while this failed; this is the assertion that closes that gap."""
    hits = _hits_via_real_chunker(_RFC_SELF_CORRECTING)
    assert any("8,871" in h.text for h in hits), "fixture lost the stale figure"
    assert any("9,806" in h.text for h in hits), "fixture lost the correct figure"

    out = render_response(_cfg(), hits, "How many RFCs are in the corpus?")

    # The outcome: the disagreement is surfaced, not silently ranked away.
    assert "CONFLICTING BINDINGS" in out, (
        "the stale 8,871 was served without surfacing that 9,806 disagrees — "
        "the exact C1 failure #9 claimed to fix and did not"
    )
    assert "8,871" in out and "9,806" in out, "both competing figures must be visible"


def test_a_single_truth_document_serves_no_conflict():
    """The negative control: a document that states one figure consistently must
    NOT trip the conflict notice — the no-conflict serve path stays clean."""
    body = (
        "# RFC full-corpus mint — EXECUTED + VERIFIED 2026-07-25\n\n"
        "**DONE.** 9,806 RFCs are now in the corpus.\n\n"
        "## Result\n\nThe corpus holds 9,806 RFCs, resolver live.\n"
    )
    out = render_response(_cfg(), _hits_via_real_chunker(body),
                          "How many RFCs are in the corpus?")
    assert "CONFLICTING BINDINGS" not in out
