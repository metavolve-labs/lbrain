"""A3 - the SERVE-LAYER arm the resolver suite does not cover (CSO, 2026-09-14T03:20Z).

WHY THIS FILE EXISTS
    The ten arms in test_a3_retired_names_its_correction.py all stop at
    `_resolve_retired_successors`. They establish that the MAP is right. My s1.1 location
    axis is not about a map -- it is about what a reader SEES:

        "attached to the retired record's OWN SERVED BLOCK - its header line or its served body"

    Between the correct map and the correct renderer sits `h.retired_successor`, assigned at
    TWO separate sites on TWO routes (search.py:704 hybrid, search.py:833 keyword), each with
    its own resolver call. Nothing asserted either one, or the rendered string.

    A build whose wiring was removed entirely would still pass all ten resolver arms: every
    record would render "RETIRED (no successor named)", which is F3's required output.

    Written by the verifier, against the property, not the implementation.
"""
from lbrain.search import Hit
from lbrain.serve import _header


def _hit(**kw):
    """NOTE on the fixture, because my first version did not reach the branch at all:
    the RETIRED render is guarded by `elif "retired" in h.boosts` (serve.py:558), a BOOSTS
    KEY -- not an `is_retired` attribute, which is what I assumed. The S1 precondition assert
    caught it: every arm failed with no RETIRED token in the output. Write the fixture from
    the trigger, and assert it reaches the branch."""
    h = Hit(rel_path="old.md", chunk_idx=0, text="t", title="Old", score=1.0)
    h.boosts = dict(getattr(h, "boosts", {}) or {}, retired=1.0)
    for k, v in kw.items():
        setattr(h, k, v)
    return h


def _render(**kw):
    return _header(1, _hit(**kw), None)


def test_S1_resolvable_successor_is_RENDERED_as_an_address():
    out = _render(retired_successor="new.md")
    assert "RETIRED" in out, "precondition: the retired token must render at all"
    assert "new.md" in out, (
        "S1: a resolved successor must appear in the RENDERED header, not only in the map. "
        "This is the arm that fails if the map->hit wiring is dropped."
    )


def test_S2_unresolvable_renders_as_NOT_a_link():
    out = _render(retired_successor="?ghost-record")
    assert "UNRESOLVABLE" in out, "S2: an unresolvable target must be marked as such"
    assert "ghost-record" in out, "S2: the unresolvable target must still be named"
    assert "corrected by" not in out, "S2: it must never render in the link form"


def test_S3_absent_declaration_renders_the_honest_state():
    out = _render()
    assert "no successor named" in out, "S3: absence must render as absence, never as silence"
    assert "corrected by" not in out


def test_S4_the_address_is_on_the_RETIRED_record_s_own_line():
    """Location axis, stated as a reader experiences it: the header for THIS record carries
    the address, so a reader served this record ALONE has it."""
    out = _render(retired_successor="new.md")
    line = next(l for l in out.splitlines() if "RETIRED" in l)
    assert "new.md" in line, (
        "S4: the address must be on the retired record's own rendered line, not elsewhere"
    )


def test_S5_countermodel_dropped_wiring_would_FAIL_S1_but_PASS_every_resolver_arm():
    """The countermodel that motivates this file. A hit whose retired_successor is never
    populated - exactly what a removed wiring produces - renders F3's output. The resolver
    suite cannot tell that apart from a correct build; S1 can."""
    dropped = _render()                       # wiring removed => attribute never set
    correct = _render(retired_successor="new.md")
    assert "no successor named" in dropped, "the dropped-wiring render is F3's output"
    assert dropped != correct, "S1 must separate dropped wiring from a correct build"
