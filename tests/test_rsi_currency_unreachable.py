"""RSI-PRELIM-1 engine landmine CUR-07: currency all-clear ignores UNREACHABLE.

A survey with unreachable records (indexed + on disk but no configured source
covers them — e.g. a lair retired by dropping it from config) must NOT certify
the index current. A fully-clean survey still must.
"""
from lbrain.index_currency import Survey


def test_cur07_unreachable_is_not_current():
    s = Survey(ran=True, unreachable=[f"retired/lair-{i}/LAIR.md" for i in range(37)])
    assert s.divergent == 0            # import would not touch them — correct
    assert s.is_current is False       # but the index is NOT trustworthy-current


def test_cur07_fully_clean_survey_still_current():   # NO-REGRESSION
    assert Survey(ran=True).is_current is True


def test_cur07_not_run_still_not_current():   # NO-REGRESSION (existing contract)
    assert Survey(ran=False).is_current is False
