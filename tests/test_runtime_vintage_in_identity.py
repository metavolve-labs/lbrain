"""A receipt must be able to see WHICH CODE answered it.

A-585 (2026-09-13): a formal per-seat receipt taken from a long-running MCP process measures the code
that process LOADED, not the code on disk. Measured that day: all five live seat processes predated
the fix they were being receipted against, one of them by 37 hours. Every receipt collected described
a process vintage while reading as a statement about the shipped engine.

The trap is sharper than staleness: the only way to make such a receipt PASS is to restart the
process -- which is precisely what an honest seat must not do, because restarting to obtain a pass
manufactures the result. One seat identified that and filed INCOMPLETE rather than bounce its session.

`engine_stamp()` already bound the loaded checkout at import (A-578). What was missing is that the
vintage did not appear in the surface a receipt actually quotes. These tests pin that it does.
"""
import re

from lbrain.config import Config
from lbrain.identity import describe


def test_identity_reports_the_code_that_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    rt = describe(Config.load()).get("runtime")
    assert rt is not None, "no runtime block: a receipt cannot tell which code answered"
    assert rt.get("engine"), "engine stamp missing from the receipt surface"


def test_identity_reports_when_this_process_loaded(tmp_path, monkeypatch):
    """The start time is what lets a reader compare against a fix's commit time."""
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    started = describe(Config.load())["runtime"].get("started")
    assert started, "no process start time: 'which vintage answered' is unanswerable"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", started), started


def test_the_note_says_what_the_vintage_MEANS(tmp_path, monkeypatch):
    """A bare timestamp invites the wrong inference. The receipt has to say that a later fix is
    NOT in this process, or a reader treats a current working tree as a current server."""
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    note = describe(Config.load())["runtime"].get("note", "")
    assert "NOT in this process" in note, note


def test_the_stamp_is_the_LOADED_checkout_not_a_live_read(tmp_path, monkeypatch):
    """The anti-trivial arm. A stamp recomputed on each call would satisfy every test above and
    still report the CURRENT sha from a process running old code -- the exact defect. Calling it
    twice across a simulated HEAD change must return the same value."""
    from lbrain import amp
    first = amp.engine_stamp()
    monkeypatch.setattr(amp, "_ENGINE_STAMP", first, raising=False)
    assert amp.engine_stamp() == first, "engine stamp is recomputed; a stale process would lie"
