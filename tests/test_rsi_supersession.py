"""RSI-PRELIM-1 cycle-1 engine landmines: supersession parse (SUP-05, L5, SUP-14).

Each test asserts the CORRECT (post-fix) behaviour, so it is RED on the pre-fix
commit and GREEN after. No-regression cases guard the legitimate path.
"""
from pathlib import Path
from lbrain.index import parse


def _sup(text, name="d"):
    """Parse a markdown string, return the list of superseded slugs."""
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, dir="/tmp") as f:
        f.write(text); p = f.name
    try:
        return parse(Path(p)).supersedes
    finally:
        os.unlink(p)


# ── SUP-05: a QUOTED / INDENTED / FENCED supersedes line mints no edge ──
def test_sup05_blockquote_mints_no_edge():
    assert _sup("# Doc\n\n> **Supersedes:** [[X]]\n") == []

def test_sup05_indented_codeblock_mints_no_edge():
    assert _sup("# Doc\n\n    **Supersedes:** [[X]]\n") == []

def test_sup05_fenced_mints_no_edge():
    assert _sup("# Doc\n\n```\n**Supersedes:** [[X]]\n```\n") == []

def test_sup05_real_declaration_at_col0_still_works():   # NO-REGRESSION
    assert _sup("# Doc\n\n**Supersedes:** [[X]]\n") == ["X"]


# ── L5: body-form bare slug (no [[ ]]) is captured, not discarded ──
def test_l5_bare_slug_body_form_captured():
    assert _sup("# Doc\n\n**Supersedes:** retired-v1\n") == ["retired-v1"]

def test_l5_bare_slug_empty_guard_holds():   # NO-REGRESSION
    assert _sup("# Doc\n\n**Supersedes:** nothing\n") == []
    assert _sup("# Doc\n\n**Supersedes:** none\n") == []
    assert _sup("# Doc\n\n**Supersedes:** -\n") == []


# ── SUP-14: frontmatter `supersedes: nothing` mints no 'nothing' edge ──
def test_sup14_frontmatter_nothing_mints_no_edge():
    assert _sup("---\nsupersedes: nothing\n---\n# Doc\n") == []

def test_sup14_frontmatter_real_slug_still_works():   # NO-REGRESSION
    assert _sup("---\nsupersedes: real-slug\n---\n# Doc\n") == ["real-slug"]

def test_sup14_frontmatter_list_empty_guard():
    assert _sup("---\nsupersedes:\n  - nothing\n  - real-one\n---\n# Doc\n") == ["real-one"]


# ── C2-12 (cycle-2): Unicode whitespace indentation + wikilink empty-guard ──
def test_c2_12_unicode_whitespace_indented_mints_no_edge():
    # U+00A0 non-breaking space
    assert _sup("# Doc\n\n\xa0Supersedes: [[X]]\n") == []
    # U+2003 em space
    assert _sup("# Doc\n\n\u2003Supersedes: [[X]]\n") == []


def test_c2_12_body_wikilink_empty_guard_mints_no_edge():
    assert _sup("# Doc\n\n**Supersedes:** [[nothing]]\n") == []
    assert _sup("# Doc\n\n**Supersedes:** [[none]]\n") == []


def test_c2_12_frontmatter_string_wikilink_empty_guard():
    assert _sup("---\nsupersedes: [[nothing]]\n---\n# Doc\n") == []


def test_c2_12_frontmatter_list_wikilink_empty_guard():
    assert _sup("---\nsupersedes:\n  - [[nothing]]\n  - [[none]]\n  - [[real-one]]\n---\n# Doc\n") == ["real-one"]



# --- C2-12 class-level closure AT THE parse() API (Touchstone's blind-verify point).
#     The indent check must hold through the REAL API, not just when _body_supersedes
#     is called directly — for EVERY indent (space/tab/nbsp/em-space), bold OR bare,
#     with OR without frontmatter. A version where parse() strips leading whitespace
#     would let all of these through and reject only `>` blockquotes. -------------
import pytest

_INDENTS = {"space": "    ", "tab": "\t", "nbsp": "\xa0", "emsp": " "}
_FORMS = {"bold": "**Supersedes:** [[X]]", "bare": "Supersedes: [[X]]"}
_FMPRE = {"noFM": "# D\n\n", "FM": "---\nt: 1\n---\n# D\n\n"}


@pytest.mark.parametrize("fm", _FMPRE)
@pytest.mark.parametrize("ind", _INDENTS)
@pytest.mark.parametrize("form", _FORMS)
def test_c2_12_indented_supersedes_mints_no_edge_through_parse(fm, ind, form):
    text = _FMPRE[fm] + _INDENTS[ind] + _FORMS[form] + "\n"
    assert _sup(text) == [], f"indented {ind}/{form}/{fm} minted an edge through parse()"


def test_c2_12_column0_declaration_still_mints():        # NO-REGRESSION
    assert _sup("# D\n\n**Supersedes:** [[X]]\n") == ["X"]
    assert _sup("# D\n\nSupersedes: [[X]]\n") == ["X"]


def test_c2_12_blockquote_still_rejected():              # NO-REGRESSION (SUP-05)
    assert _sup("# D\n\n> **Supersedes:** [[X]]\n") == []
