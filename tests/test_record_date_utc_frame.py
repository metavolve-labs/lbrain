"""A2 date-frame regression: the served date label must be the UTC date, not the host's.

CSO A2 run 2026-09-11: a record whose mtime was 2026-09-11T05:26:18Z served `file-dated 2026-09-10`
on a UTC-7 host. The file did not exist at any instant of 2026-09-10 UTC.

This file was first committed as a standalone script with module-level `sys.exit()`. Under pytest that
aborts COLLECTION for the whole suite with INTERNALERROR, so a regression test for one defect disabled
every other test in the repo. The CCO caught it. The commit that introduced it also claimed "840 passed",
which was true of the code change and not of the commit, because the suite was run before the file was
added. A test that has never been run by the runner that will run it is not a test.
"""
import datetime
import os
import time

import pytest

# the exact instant from the run: late in a UTC day, previous day on a UTC-7 host
RUN_TS = datetime.datetime(2026, 9, 11, 5, 26, 18, tzinfo=datetime.timezone.utc).timestamp()
SERVE_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lbrain", "serve.py")


def _iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()


@pytest.fixture
def tz_utc_minus_7():
    """Force the host zone so the skew is reproducible on any machine, then restore it."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    if prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = prev
    if hasattr(time, "tzset"):
        time.tzset()


def test_utc_frame_gives_the_true_date():
    assert _iso_utc(RUN_TS) == "2026-09-11"


def test_local_frame_reproduces_the_defect(tz_utc_minus_7):
    """The old call read a day early on this zone. Pinning it keeps the defect describable."""
    if not hasattr(time, "tzset"):
        pytest.skip("cannot force the host zone on this platform")
    assert datetime.date.fromtimestamp(RUN_TS).isoformat() == "2026-09-10"
    assert _iso_utc(RUN_TS) == "2026-09-11"


def test_serve_resolves_in_utc_and_keeps_no_local_frame_call():
    src = open(SERVE_PY, encoding="utf-8").read()
    assert "datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()" in src
    assert "return datetime.date.fromtimestamp(ts).isoformat()" not in src


def test_affected_window_is_the_first_utc_hours_on_a_negative_offset_host():
    """The CSO reported the last seven hours, the CTO repeated it, the CCO checked the arithmetic."""
    tz = datetime.timezone(datetime.timedelta(hours=-7))
    early = [h for h in range(24)
             if datetime.datetime(2026, 9, 11, h, 30, tzinfo=datetime.timezone.utc).astimezone(tz).date()
             < datetime.date(2026, 9, 11)]
    assert early == [0, 1, 2, 3, 4, 5, 6]


def test_record_date_behaviour_not_just_source(tz_utc_minus_7):
    """CALL record_date and assert the label it returns.

    The CCO's standing point, made hours before this file existed: counting a source-level check as an
    operational result is one of the team's three biggest avoidable costs. The first version of this file
    asserted that a string appears in serve.py, which proves the edit landed and proves nothing about what
    a reader is served. This constructs a Hit with the run's exact mtime and asserts the returned tuple.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("cannot force the host zone on this platform")
    from lbrain.search import Hit
    from lbrain.serve import record_date

    h = Hit(rel_path="_META/zz-a2-frame-probe.md", chunk_idx=0, text="probe", title="probe",
            score=1.0, mtime=RUN_TS)
    label, date = record_date(h)

    # the basis is still named honestly...
    assert label == "file-dated"
    # ...and the date is now the UTC one, not the host's previous day
    assert date == "2026-09-11", (
        "record_date returned %r; on a UTC-7 host the pre-fix code returned 2026-09-10 "
        "for a file that did not exist at any instant of that UTC day" % date
    )
