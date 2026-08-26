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
