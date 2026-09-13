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


def test_whoami_human_surface_renders_the_process_vintage():
    """A-585 part 2: the vintage must appear on the DEFAULT surface, not only --json.

    The runtime block shipped in describe() and was rendered nowhere a receipt is read from:
    invisible without --json on the CLI, and absent entirely from a pinned MCP server. An
    instrument for "which code answered?" that cannot answer it on the default surface is
    undeployed, not shipped.
    """
    from click.testing import CliRunner
    from lbrain.cli import main as cli

    res = CliRunner().invoke(cli, ["whoami"])
    assert res.exit_code == 0, res.output
    out = res.output

    # the human surface names the vintage and warns what it means
    assert "this process" in out
    assert "engine:" in out
    assert "started:" in out
    assert "NOT in this process" in out


def test_whoami_vintage_is_not_satisfied_by_any_timestamp_on_the_page():
    """ANTI-TRIVIAL ARM. The test above passes if the page merely contains a time somewhere.

    A build date, an epoch id or a coverage_checked_at would all satisfy a naive check while
    telling the reader nothing about WHICH CODE answered. This pins the two facts that only a
    live process can report -- its own start time and its own engine stamp -- and requires them
    to match this interpreter rather than any string that looks like a date.
    """
    import os
    from click.testing import CliRunner
    from lbrain.cli import main as cli
    from lbrain.identity import _proc_started, _engine_stamp_safe

    res = CliRunner().invoke(cli, ["whoami"])
    out = res.output

    started = _proc_started()
    if started:
        # the rendered start time is THIS process's, not a corpus/build timestamp
        assert started in out, f"whoami must render this process's own start {started!r}"

    stamp = _engine_stamp_safe()
    if stamp:
        assert stamp in out, f"whoami must render this process's own engine stamp {stamp!r}"
