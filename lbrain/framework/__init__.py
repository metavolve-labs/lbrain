"""The lair authoring framework — shipped WITH the tool, on purpose.

These documents are the authoring contract for the corpus LBrain ranks over.
Ranking quality is bounded by corpus quality: a perfect retriever over a corpus
of confident guesses returns confident guesses, faster. Shipping the engine
without the contract ships half a product.

They previously lived in docs/ and reached no user — the wheel carried zero
markdown and the README link pointed at a private repository (A-408).
"""
from __future__ import annotations

from pathlib import Path

DOCS = {
    "AUTHORING_DISCIPLINE": "How to write records that stay true (start here)",
    "LAIR_RULES": "The structural authoring contract — caps, sections, naming",
    "LAIR_TEMPLATE": "A blank lair to copy",
    "FAST_START_PROTOCOL": "Bootstrapping a lair system from an existing project",
    "SELF_FILLING_PROTOCOL": "Letting the corpus accumulate without manual curation",
}


def path(name: str) -> Path:
    """Absolute path to a framework doc, by bare name (no .md)."""
    p = Path(__file__).parent / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"no framework doc named {name!r}; have: {', '.join(DOCS)}")
    return p


def read(name: str) -> str:
    return path(name).read_text(encoding="utf-8")
