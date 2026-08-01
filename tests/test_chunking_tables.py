"""A-412 — chunking must not orphan table rows.

The house style mandates tables over prose, and structured serving displays chunk
text verbatim. A continuation chunk carrying rows with no header row is not
merely degraded — `| 0.25 | ✅ |` is uninterpretable — and it reaches the model
exactly as stored.
"""
import re

from lbrain.index import Doc, chunk

_ROW = re.compile(r"^\s*\|.*\|\s*$")


def _doc(body: str) -> Doc:
    from pathlib import Path
    return Doc(path=Path("/tmp/t.md"), rel_path="t.md", title="T", body=body,
               metadata={}, wikilinks=[], supersedes=[], doc_hash="h", mtime=0.0,
               is_priority=False, doc_type="project", metadata_ok=True, disclosure="")


def _big_table(rows: int) -> str:
    head = "## Register\n\n| ID | Finding | Consequence | Date |\n|----|---------|-------------|------|\n"
    body = "".join(
        f"| A-{i:03d} | finding number {i} with enough text to consume budget | "
        f"consequence text for row {i} spelled out at length | 2026-08-01 |\n"
        for i in range(rows)
    )
    return head + body


def test_large_table_produces_multiple_chunks():
    cs = chunk(_doc(_big_table(120)), max_tokens=256, overlap=32)
    assert len(cs) > 1, "test is vacuous unless the table actually splits"


def test_every_chunk_of_a_table_carries_its_header():
    cs = chunk(_doc(_big_table(120)), max_tokens=256, overlap=32)
    for c in cs:
        rows = [ln for ln in c.text.split("\n") if _ROW.match(ln)]
        if not rows:
            continue
        assert "| ID | Finding | Consequence | Date |" in c.text, (
            f"chunk {c.chunk_idx} has {len(rows)} table rows but no header:\n{c.text[:200]}"
        )


def test_no_chunk_starts_or_ends_mid_row():
    cs = chunk(_doc(_big_table(120)), max_tokens=256, overlap=32)
    for c in cs:
        for ln in c.text.split("\n"):
            if ln.lstrip().startswith("|"):
                assert ln.rstrip().endswith("|"), f"row cut mid-line: {ln!r}"


def test_prose_sections_are_not_split_mid_line():
    body = "## Notes\n\n" + "".join(
        f"This is sentence number {i} in a long prose section without any tables.\n"
        for i in range(200)
    )
    cs = chunk(_doc(body), max_tokens=256, overlap=32)
    assert len(cs) > 1
    for c in cs:
        for ln in c.text.split("\n"):
            if ln.strip():
                assert ln.startswith("This is sentence") or ln.startswith("## "), (
                    f"line was cut: {ln!r}"
                )


def test_small_doc_is_untouched_by_the_new_path():
    # Sections that fit never reach _window_section — pins the blast radius.
    body = "## Small\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    cs = chunk(_doc(body), max_tokens=512, overlap=64)
    assert len(cs) == 1
    assert cs[0].text.strip() == body.strip()


def test_single_oversized_line_still_slices_rather_than_hanging():
    body = "## Big\n\n" + ("word " * 4000) + "\n"
    cs = chunk(_doc(body), max_tokens=128, overlap=16)
    assert len(cs) > 1
    assert all(c.token_count <= 128 for c in cs)
