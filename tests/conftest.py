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

import hashlib
import os
import sys
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
    # Capture the REAL install before touching the environment. _real_home() reads
    # LBRAIN_HOME, so computing it after the setenv below returns the temp dir, and
    # every "is this path under the real home?" test then answers no — leaving the
    # repoint loop a silent no-op that looks like it ran.
    real = _real_home().resolve()

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

    # Repoint EVERY module-level Path that lies under the real install, discovered
    # rather than enumerated.
    #
    # The named-constant version patched CONFIG_DIR and CONFIG_PATH and missed
    # ENV_PATH and DB_PATH — all four bind at import time in config.py:16-19, so
    # re-pointing CONFIG_DIR moves none of the others. On 2026-08-01 that let
    # `test_config_roundtrip.py` (which runs `lbrain init --gemini-key
    # explicitly-passed`) write the literal fixture string into the operator's
    # real ~/.lbrain/env, destroying a live Gemini credential that existed in
    # exactly one place and had never been backed up.
    #
    # Enumeration is the bug: it is a list someone must remember to extend, and
    # the file it protects gains constants over time. Discovery covers whatever
    # exists now and whatever is added later, which is the difference between
    # fixing the instance and fixing the class.
    for name, mod in list(sys.modules.items()):
        if not (name == "lbrain" or name.startswith("lbrain.")) or mod is None:
            continue
        for attr in dir(mod):
            if attr.startswith("__"):
                continue
            try:
                val = getattr(mod, attr)
            except Exception:
                continue
            if not isinstance(val, Path):
                continue
            try:
                rel = val.resolve().relative_to(real)
            except (ValueError, OSError):
                continue
            monkeypatch.setattr(mod, attr, home / rel, raising=False)

    return home


def pytest_configure(config):
    """Refuse to run at all if the suite can see a REAL install.

    The guard below is a tripwire — it tells you AFTER the damage. This is the gate:
    on 2026-08-01 a test wrote `explicitly-passed` over a live GEMINI_API_KEY because
    one of four import-time paths was unpatched. Isolation is now generic, but
    isolation is a mechanism that can regress; "there is nothing here to destroy" is
    a property that cannot.

    Set LBRAIN_TEST_HOME (or LBRAIN_HOME) to a scratch dir before running. Opt out
    with LBRAIN_ALLOW_REAL_HOME=1 only if you genuinely mean to test against a live
    install and have backed up `env` — which nothing did until it was too late.
    """
    if os.environ.get("LBRAIN_ALLOW_REAL_HOME") == "1":
        return
    home = Path(os.environ.get("LBRAIN_HOME") or (Path.home() / ".lbrain"))
    secrets = home / "env"
    db = home / "brain.db"
    if not (secrets.exists() or db.exists()):
        return
    raise pytest.UsageError(
        f"\nREFUSING TO RUN: {home} looks like a REAL LBrain install "
        f"({'env ' if secrets.exists() else ''}{'brain.db' if db.exists() else ''}).\n"
        f"A test suite must never be able to reach live credentials.\n\n"
        f"  LBRAIN_HOME=$(mktemp -d) python3 -m pytest tests/\n\n"
        f"Override only if you mean it: LBRAIN_ALLOW_REAL_HOME=1\n"
    )


def _fingerprint(home: Path) -> dict[str, str]:
    """Content hash of every file in the real install.

    The previous guard diffed ONLY config.toml, so it passed cleanly while a test
    was overwriting the operator's live API key in the sibling `env` file — a
    tripwire across one doorway of a building with four. Its own docstring had the
    right instinct ("a green suite is not evidence that nothing was written") and
    then applied it to a single path.

    Hash rather than mtime: a write that restores identical bytes is not damage,
    and a write that changes bytes within the same second is.
    """
    out: dict[str, str] = {}
    if not home.exists():
        return out
    for p in sorted(home.rglob("*")):
        if not p.is_file():
            continue
        try:
            out[str(p.relative_to(home))] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[str(p.relative_to(home))] = "<unreadable>"
    return out


@pytest.fixture(autouse=True, scope="session")
def guard_real_install():
    """Fail the run if the real install was modified, rather than discovering it later.

    A tripwire, not a mechanism: if this ever fires, the isolation above has a
    hole and the fix belongs there. The assertion runs even when tests pass,
    because the damaging run in the incident above *failed* — a green suite is
    not evidence that nothing was written.
    """
    home = _real_home()
    before = _fingerprint(home)
    yield
    after = _fingerprint(home)
    changed = sorted(
        {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    )
    assert not changed, (
        "A test modified the real LBrain install at "
        f"{home}: {changed}\n"
        "Test isolation has a hole — fix tests/conftest.py, not the symptom."
    )
