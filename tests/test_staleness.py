"""Perishable-claim detection.

The regression that motivates every case here: a lair asserting DELINQUENT was
served correctly and was false. The detector must fire on how our corpus
actually writes status (emphasis and table cells, per LAIR_RULES) and must NOT
fire on the same words used as ordinary prose or inside code examples — a naive
keyword list matches 46% of documents and is worthless.
"""
import datetime

from lbrain.staleness import (claim_date, days_since, expired, is_excluded,
                              open_claims, volatility)

TODAY = datetime.date(2026, 7, 27)


class TestMarkerFires:
    def test_table_cell_with_emoji(self):
        assert volatility("| ⚠️ **DELINQUENT** — was due 2026-03-01 |") == "open"

    def test_bold_status_header(self):
        assert volatility("**Status**: ⏳ **PENDING** counsel review") == "open"

    def test_bare_bold(self):
        assert volatility("The filing is **NOT FILED** as of writing") == "open"

    def test_lair_protocol_status_enum(self):
        assert volatility("**Status**: ACTIVE — mid-migration") == "open"


class TestMarkerStaysQuiet:
    def test_json_example(self):
        """Drove the fire rate to 46% before the line filter."""
        assert volatility('  "status": "pending",') == ""

    def test_prose_mention(self):
        assert volatility("resolving the pending question of scope") == ""

    def test_ordinary_word(self):
        assert volatility("the price is current and the node is live") == ""

    def test_author_opt_out(self):
        assert volatility("volatile: false\n\n**Status**: ACTIVE") == ""


class TestClaimDate:
    def test_last_updated_beats_filename(self):
        t = "**Last Updated**: 2026-07-09\n"
        assert claim_date(t, "note-2026-01-01.md", "2026-07-27") == ("verified", "2026-07-09")

    def test_as_of_takes_the_newest_not_the_first(self):
        t = "As of 2026-01-15 the pipeline ran.\nAs of 2026-06-03 it was replaced."
        assert claim_date(t, "abstraction-x.md", "2026-07-27") == ("as-of", "2026-06-03")

    def test_filename_date_beats_mtime(self):
        assert claim_date("no headers", "project-x-2026-05-15.md", "2026-07-27") \
            == ("dated", "2026-05-15")

    def test_mtime_is_labelled_as_the_weak_signal_it_is(self):
        """mtime moves on a typo fix; it must never be called a claim date."""
        assert claim_date("no headers", "LAIR.md", "2026-07-27") == ("file-dated", "2026-07-27")


class TestDecidableTier:
    def test_passed_verify_by_is_provable(self):
        assert expired("verify_by: 2026-06-30\n", TODAY) == "2026-06-30"

    def test_future_verify_by_is_not_stale(self):
        assert expired("verify_by: 2026-12-01\n", TODAY) is None

    def test_malformed_date_does_not_crash(self):
        assert expired("verify_by: soon\n", TODAY) is None


class TestExclusions:
    def test_archive_paths_are_stale_by_design(self):
        assert is_excluded("_archive/completed/old/LAIR.md")
        assert is_excluded("X/_archive_legacy/thing.md")
        assert not is_excluded("X-METAVOLVE-CORP/state-compliance-de-ca/LAIR.md")


class TestArithmetic:
    def test_days_since(self):
        assert days_since("2026-07-09", TODAY) == 18   # the Delaware gap

    def test_garbage_returns_none(self):
        assert days_since("not-a-date", TODAY) is None


def test_the_delaware_case_end_to_end():
    """The exact record, as written on 2026-07-09, must be caught at 18 days."""
    doc = ("# Metavolve Labs, Inc. — DE Franchise Tax\n"
           "**Status**: ACTIVE — DE is DELINQUENT (was due 2026-03-01)\n"
           "**Last Updated**: 2026-07-09\n\n"
           "| DE annual report + franchise tax | ⚠️ **DELINQUENT** — was due **2026-03-01** |\n")
    assert volatility(doc) == "open"
    label, date = claim_date(doc, "state-compliance-de-ca/LAIR.md", "2026-07-27")
    assert (label, date) == ("verified", "2026-07-09")
    assert days_since(date, TODAY) == 18
    assert any("DELINQUENT" in c for c in open_claims(doc))
