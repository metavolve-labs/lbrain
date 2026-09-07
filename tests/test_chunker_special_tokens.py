"""A document that MENTIONS a tokenizer special token is still a document.

2026-09-07: one lair file quoting the literal ``<|endoftext|>`` made tiktoken raise inside the
chunker, which aborted ``lbrain import`` — and with it every seat's mandated daily
``lbrain epoch build`` (CIO wear script red, CTO brain stale). The chunker uses tiktoken only to
COUNT tokens for budgeting; special-token literals in prose are text and must count as text.
"""
from pathlib import Path

from lbrain.index import Doc, chunk


def _doc(body: str) -> Doc:
    return Doc(path=Path("/tmp/t.md"), rel_path="t.md", title="T", body=body,
               metadata={}, wikilinks=[], supersedes=[], doc_hash="h", mtime=0.0,
               is_priority=False, doc_type="project", metadata_ok=True, disclosure="")


SPECIAL = ["<|endoftext|>", "<|im_end|>", "<|im_start|>", "<|endofprompt|>"]


def test_chunker_accepts_special_token_literals():
    body = "# Pitfalls\n\n" + "\n\n".join(
        f"The native `{tok}` token may be mapped incorrectly in quantized runtimes." for tok in SPECIAL
    )
    chunks = chunk(_doc(body))
    assert chunks, "a document mentioning a special token must still yield chunks"
    joined = "\n".join(c.text for c in chunks)
    for tok in SPECIAL:
        assert tok in joined, f"{tok} must survive chunking verbatim"


def test_chunker_special_token_in_oversized_line():
    # exercises the single-line-larger-than-budget slicer, the third encode site
    line = ("<|endoftext|> " + "word " * 40) * 60
    chunks = chunk(_doc("# Big\n\n" + line), max_tokens=200, overlap=32)
    assert len(chunks) > 1
    assert any("<|endoftext|>" in c.text for c in chunks)
