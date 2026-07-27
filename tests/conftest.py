"""Test isolation — no test may touch a real LBrain install.

This exists because a test wrote to a live `~/.lbrain/config.toml` on 2026-07-27:
it monkeypatched `lbrain.cli`'s path constants but not `lbrain.config`'s, and
`Config.write()` resolves from the latter. The run silently rewrote the real
install's embedding provider, model, dimension, and — worse — its `sources`
list, repointing a 9,704-chunk brain at a pytest temp directory.

Nothing was lost (the vector-drift guard refused the next `embed`, which is what
caught it), but the lesson is that "remember to patch both modules" is an
implicit requirement, and implicit requirements get forgotten. So isolation is
now automatic and enforced rather than per-test and remembered.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _real_home() -> Path:
    """The install this machine would use if nothing were patched."""
    return Path(os.environ.get("LBRAIN_HOME") or (Path.home() / ".lbrain"))


@pytest.fixture(autouse=True)
def isolate_lbrain_home(tmp_path, monkeypatch):
    """Point every path-resolving module at a per-test directory.

    Autouse: a test author cannot forget it. Both `lbrain.config` and
    `lbrain.cli` are patched because `from .config import CONFIG_DIR` binds a
    separate name in the importing module — patching one leaves the other
    pointing at the user's real install.
    """
    home = tmp_path / "lbrain-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LBRAIN_HOME", str(home))

    import lbrain.config as config

    mods = [config]
    try:
        import lbrain.cli as cli

        mods.append(cli)
    except Exception:  # pragma: no cover - cli deps are optional in some envs
        pass

    for mod in mods:
        if hasattr(mod, "CONFIG_DIR"):
            monkeypatch.setattr(mod, "CONFIG_DIR", home, raising=False)
        if hasattr(mod, "CONFIG_PATH"):
            monkeypatch.setattr(mod, "CONFIG_PATH", home / "config.toml", raising=False)

    return home


@pytest.fixture(autouse=True, scope="session")
def guard_real_install():
    """Fail the run if a real config was modified, rather than discovering it later.

    A tripwire, not a mechanism: if this ever fires, the isolation above has a
    hole and the fix belongs there. The assertion runs even when tests pass,
    because the damaging run in the incident above *failed* — a green suite is
    not evidence that nothing was written.
    """
    cfg = _real_home() / "config.toml"
    before = cfg.read_bytes() if cfg.exists() else None
    yield
    after = cfg.read_bytes() if cfg.exists() else None
    assert before == after, (
        f"A test modified the real LBrain config at {cfg}. Test isolation has a "
        "hole — fix tests/conftest.py, not the symptom."
    )
