"""The served footer names which code answered (CSO A3 parity case 2026-09-11T07:31Z: a stale MCP
process served the original defect with no signal that a repaired build existed)."""
import re

from lbrain import __version__, amp


def test_engine_stamp_is_version_plus_short_sha_from_a_checkout():
    st = amp.engine_stamp()
    assert st.startswith(__version__)
    # this test runs from the checkout, so the sha suffix must be present and 7 hex chars
    assert re.fullmatch(re.escape(__version__) + r"\+[0-9a-f]{7}", st), st


def test_provenance_footer_carries_the_stamp():
    foot = amp.provenance([], 0, 0, 0)
    assert "· engine " + amp.engine_stamp() in foot


def test_stamp_is_cached_per_process():
    assert amp.engine_stamp() is amp.engine_stamp()
