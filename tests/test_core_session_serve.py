"""core_memory_serve = "session" — opt-in per-process dedup of the always-on core block.

The invariants: default behavior is byte-identical to before (always full); session mode
serves full once then a marker; an EDIT always propagates (staleness is worse than spend);
the periodic refresh bounds a compaction outage; a disclosure-mode switch re-serves.
"""

from __future__ import annotations

import os
import time

import pytest

from lbrain import amp


@pytest.fixture()
def core_file(tmp_path):
    p = tmp_path / "CORE.md"
    p.write_text("- Who: test persona\n- Rule: verify before asserting\n")
    return str(p)


@pytest.fixture(autouse=True)
def clean_session_state():
    amp._CORE_SESSION.clear()
    yield
    amp._CORE_SESSION.clear()


def test_default_always_serves_full_every_call(core_file):
    first = amp.core_block(core_file, 900)
    second = amp.core_block(core_file, 900)
    assert first == second
    assert "verify before asserting" in second


def test_session_mode_serves_full_once_then_marker(core_file):
    first = amp.core_block(core_file, 900, serve="session")
    second = amp.core_block(core_file, 900, serve="session")
    assert "verify before asserting" in first
    assert "verify before asserting" not in second
    assert "served in full earlier this session" in second
    assert "unchanged" in second


def test_session_mode_edit_always_propagates(core_file):
    amp.core_block(core_file, 900, serve="session")
    with open(core_file, "a") as f:
        f.write("- NEW: a correction appended last\n")
    # mtime granularity can be coarse; force a distinct mtime rather than sleeping.
    st = os.stat(core_file)
    os.utime(core_file, (st.st_atime, st.st_mtime + 2))
    third = amp.core_block(core_file, 900, serve="session")
    assert "a correction appended last" in third  # full re-serve, marker skipped


def test_session_mode_periodic_refresh_bounds_the_outage(core_file):
    amp.core_block(core_file, 900, serve="session")
    full_serves = 1
    for _ in range(2 * amp._CORE_REFRESH_EVERY):
        out = amp.core_block(core_file, 900, serve="session")
        if "verify before asserting" in out:
            full_serves += 1
    # Two full windows → at least two periodic refreshes beyond the first serve.
    assert full_serves >= 3


def test_session_mode_is_keyed_per_budget(core_file):
    amp.core_block(core_file, 900, serve="session")
    other_budget = amp.core_block(core_file, 400, serve="session")
    # A different budget is a different delivery — it must serve full, not a marker.
    assert "served in full earlier" not in other_budget


def test_marker_never_leaks_core_content(core_file):
    amp.core_block(core_file, 900, serve="session")
    marker = amp.core_block(core_file, 900, serve="session")
    assert "test persona" not in marker and "Rule:" not in marker
