"""Dial-in v0: interview prompt, templates, manifest, drift.

All tests run under the autouse isolate_lbrain_home fixture; dialin resolves
CONFIG_DIR late (never at import), so the manifest lands in the tmp home.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lbrain import dialin
from lbrain.cli import main


# --------------------------------------------------------------------------- #
# Interview prompt                                                             #
# --------------------------------------------------------------------------- #

def test_prompt_carries_all_nine_questions_and_the_receipt():
    for anchor in ("SOURCES", "EMBEDDINGS", "RECALL-FIRST HOOK", "AUTO RE-SYNC",
                   "AGENT MEMORY", "CORE MEMORY", "MCP", "SECRET HYGIENE",
                   "HISTORY IMPORT", "RECEIPT"):
        assert anchor in dialin.INTERVIEW_PROMPT, f"missing: {anchor}"


def test_prompt_binds_the_agent_to_additive_and_recorded_steps():
    assert "ADDITIVE steps only" in dialin.INTERVIEW_PROMPT
    assert "lbrain setup record" in dialin.INTERVIEW_PROMPT
    # identity is offered last and never gates the free path
    assert "never a gate" in dialin.INTERVIEW_PROMPT


def test_bare_setup_command_prints_the_interview():
    res = CliRunner().invoke(main, ["setup"])
    assert res.exit_code == 0
    assert "LBRAIN DIAL-IN" in res.output
    assert "SECRET HYGIENE" in res.output


# --------------------------------------------------------------------------- #
# Templates                                                                    #
# --------------------------------------------------------------------------- #

def test_templates_written_executable_and_warn_by_default(tmp_path):
    written = dialin.write_templates(tmp_path / "hooks")
    names = {p.name for p in written}
    assert {"lbrain-recall-first.sh", "lbrain-autosync.sh",
            "claude-code-settings-snippet.json", "README.md"} <= names
    recall = (tmp_path / "hooks" / "lbrain-recall-first.sh")
    assert recall.stat().st_mode & 0o111, "hook must be executable"
    body = recall.read_text(encoding="utf-8")
    # nudge posture is the DEFAULT; blocking is the opt-in — decision #1,
    # onboarding lair 2026-08-09
    assert 'LBRAIN_FIRST_MODE:-warn' in body
    assert "__LBRAIN_SOURCE_DIRS__" in body  # placeholder must survive verbatim


def test_templates_cli_points_at_the_placeholder(tmp_path):
    res = CliRunner().invoke(main, ["setup", "templates", "--dir", str(tmp_path / "h")])
    assert res.exit_code == 0
    assert "__LBRAIN_SOURCE_DIRS__" in res.output
    snippet = (tmp_path / "h" / "claude-code-settings-snippet.json").read_text()
    assert str(tmp_path / "h") in snippet  # snippet references its own dir


# --------------------------------------------------------------------------- #
# Manifest                                                                     #
# --------------------------------------------------------------------------- #

def test_record_roundtrip_and_idempotence(tmp_path):
    hook = tmp_path / "a-hook.sh"
    hook.write_text("#!/bin/sh\n")
    assert dialin.record_step("hook", "recall-first hook",
                              path=str(hook), undo=f"rm {hook}") is True
    assert dialin.record_step("hook", "recall-first hook",
                              path=str(hook), undo=f"rm {hook}") is False
    entries = dialin.read_manifest()
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "hook" and e["path"] == str(hook)
    assert e["undo"] == f"rm {hook}"
    assert dialin.manifest_path().read_text(encoding="utf-8").startswith(
        "# LBrain setup manifest")


def test_record_rejects_unknown_kind():
    import pytest
    with pytest.raises(ValueError):
        dialin.record_step("gadget", "nope")


def test_drift_flags_vanished_artifacts_only(tmp_path):
    kept = tmp_path / "kept.sh"
    kept.write_text("#!/bin/sh\n")
    gone = tmp_path / "gone.sh"
    gone.write_text("#!/bin/sh\n")
    dialin.record_step("hook", "kept hook", path=str(kept), undo="rm kept")
    dialin.record_step("hook", "doomed hook", path=str(gone), undo="rm gone")
    dialin.record_step("other", "pathless step")  # path '-' never drifts
    assert dialin.drift_check() == []
    gone.unlink()
    warnings = dialin.drift_check()
    assert len(warnings) == 1 and "gone.sh" in warnings[0]


def test_status_cli_shows_entries_and_drift(tmp_path):
    runner = CliRunner()
    res = runner.invoke(main, ["setup", "status"])
    assert "no dial-in manifest" in res.output

    hook = tmp_path / "h.sh"
    hook.write_text("#!/bin/sh\n")
    runner.invoke(main, ["setup", "record", "hook", "test hook",
                         "--path", str(hook), "--undo", f"rm {hook}"])
    res = runner.invoke(main, ["setup", "status"])
    assert res.exit_code == 0 and "test hook" in res.output
    assert "artifacts present" in res.output

    hook.unlink()
    res = runner.invoke(main, ["setup", "status"])
    assert "GONE" in res.output


def test_doctor_surfaces_setup_drift(tmp_path):
    hook = tmp_path / "h.sh"
    hook.write_text("#!/bin/sh\n")
    dialin.record_step("hook", "doctor-visible hook", path=str(hook), undo="rm")
    hook.unlink()
    res = CliRunner().invoke(main, ["doctor", "--json"])
    import json
    payload = json.loads(res.output)
    assert any("doctor-visible hook" in w for w in payload["setup_drift"])
