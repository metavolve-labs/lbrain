"""`lbrain init`'s advice must be true for the situation the user is actually in.

An existing brain keeps its provider on purpose, so a bare `--gemini-key` does not
switch it. The note printed in that case used to say "an API key was found in your
environment … pass it explicitly", which was wrong twice: the key had been passed
explicitly, on the command line, and repeating that command would fail identically.
"""

from __future__ import annotations

import pytest

pytest.importorskip("click")
from click.testing import CliRunner  # noqa: E402

from lbrain.cli import main  # noqa: E402


def _init(runner, *args, env=None):
    return runner.invoke(main, ["init", *args], env=env or {}, catch_exceptions=False)


def test_fresh_install_defaults_to_on_device(tmp_path):
    res = _init(CliRunner(), "--source", str(tmp_path))
    assert res.exit_code == 0
    assert "provider: local" in res.output


def test_key_on_command_line_against_existing_brain_names_the_provider_flag(tmp_path):
    runner = CliRunner()
    _init(runner, "--source", str(tmp_path))          # existing on-device brain
    res = _init(runner, "--gemini-key", "FAKE_KEY")   # explicit, but not applied

    assert res.exit_code == 0
    assert "provider: local" in res.output            # unchanged, deliberately
    # It must not claim the key came from the environment...
    assert "found in your environment" not in res.output
    # ...and it must point at the flag that actually works, not repeat the failure.
    assert "--provider gemini" in res.output
    assert "embed --stale" in res.output


def test_key_only_in_environment_explains_the_consent_rule(tmp_path):
    res = _init(CliRunner(), "--source", str(tmp_path), env={"GEMINI_API_KEY": "FAKE_ENV_KEY"})

    assert res.exit_code == 0
    assert "provider: local" in res.output
    assert "not consent" in res.output
    assert "--provider gemini" in res.output


def test_explicit_provider_switches(tmp_path):
    runner = CliRunner()
    _init(runner, "--source", str(tmp_path))
    res = _init(runner, "--provider", "gemini", "--gemini-key", "FAKE_KEY")

    assert res.exit_code == 0
    assert "provider: gemini" in res.output
