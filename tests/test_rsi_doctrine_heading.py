"""RSI-PRELIM-1 engine landmine AX-04: 'doctrine' in a heading defeats blinding.

_DOCTRINE_HEADING_RE promoted ANY heading containing 'doctrine' to always-on
doctrine, so `## Vendor doctrine notes\\n<mutable claim>` leaked a mutable claim
through independent/adversarial blinding. A doctrine section must signal always-on
/binding intent (as both canonical CORE headings do); a bare name-match must not.
"""
from lbrain.disclosure import split_core


def test_ax04_vendor_doctrine_heading_does_not_promote_mutable_claim():
    doctrine, context = split_core(
        "## Vendor doctrine notes\n"
        "Matrix B shows 3.8% -> 95.8% (a mutable project claim).\n"
    )
    assert "95.8%" not in doctrine, "a heading merely CONTAINING 'doctrine' must not promote"
    assert "95.8%" in context


def test_ax04_canonical_alwayson_doctrine_still_delivered():   # NO-REGRESSION
    doctrine, _ = split_core(
        "## Doctrine — always delivered, in every disclosure mode\n"
        "- Never fabricate; report faithfully.\n"
    )
    assert "Never fabricate" in doctrine


def test_ax04_binding_alwayson_form_still_delivered():   # NO-REGRESSION (Agent-X shared)
    doctrine, _ = split_core(
        "## Binding doctrine — every persona, always on\n"
        "- Verify live state before asserting.\n"
    )
    assert "Verify live state" in doctrine


def test_ax04_bare_doctrine_heading_still_delivered():   # NO-REGRESSION
    doctrine, _ = split_core("## Doctrine\n- Proof-first.\n")
    assert "Proof-first" in doctrine


# --- C2-09 (cycle-2): the conjunction `doctrine AND (always|binding|every|standing)`
#     via lookaheads still promoted ADVERSARIAL headings where doctrine is not the
#     declared subject or the trigger word is a negation/mutable verb. A real
#     declaration test: doctrine must be the LEADING subject with an always-on
#     affirmation, fail-CLOSED to context otherwise. -------------------------------
def _leaks(heading):
    doctrine, context = split_core(f"{heading}\nSECRET mutable claim 95.8 percent.\n")
    return "95.8" in doctrine, "95.8" in context


def test_c2_09_standing_doctrine_for_vendor_does_not_promote():
    in_doc, in_ctx = _leaks("## Standing doctrine for the vendor")
    assert not in_doc and in_ctx, "'Standing doctrine …' must not promote — doctrine is not the subject"


def test_c2_09_not_always_doctrine_does_not_promote():
    in_doc, in_ctx = _leaks("## Not always doctrine")
    assert not in_doc and in_ctx, "a negated 'doctrine' heading must not promote"


def test_c2_09_project_doctrine_always_changes_does_not_promote():
    in_doc, in_ctx = _leaks("## Project doctrine always changes")
    assert not in_doc and in_ctx, "a mutable-verb 'doctrine' heading must not promote"


def test_c2_09_doctrine_subject_without_affirmation_does_not_promote():
    # doctrine IS the leading subject, but there is no always-on affirmation → fail-closed
    in_doc, in_ctx = _leaks("## Doctrine — vendor notes for Q3")
    assert not in_doc and in_ctx, "'Doctrine — <non-affirming>' must fail closed to context"


def test_c2_09_lowercase_canonical_still_delivered():   # NO-REGRESSION (case-insensitive affirmation)
    doctrine, _ = split_core("## doctrine — always on, every persona\n- keep this rule.\n")
    assert "keep this rule" in doctrine
