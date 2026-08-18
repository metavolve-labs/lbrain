"""`lbrain --version` must report the version that actually shipped.

`pyproject.toml` moved to 0.1.5; `lbrain/__init__.py` stayed at 0.1.4, last
touched by the v0.1.4 release commit. So 0.1.5 installs answered `--version`
with **0.1.4** — and `--version` is the first thing anyone is asked for in a bug
report, the number that decides whether a fix is present.

The same shape as every other anomaly in this repo: an assertion (the version
string in the source) and the fact it claims to describe (the version that was
built) drifting apart with nothing comparing them. Two places held the number
and only one was on the release checklist.

The fix is not the bump — it is this test. A release cut that forgets the second
file now fails here instead of at a user, which is the difference between a
checklist item and a guarantee.
"""
from __future__ import annotations

import pathlib

import lbrain

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 backport
    import tomli as tomllib

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_dunder_version_matches_pyproject():
    if not PYPROJECT.exists():  # installed from a wheel, not a checkout
        return
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert lbrain.__version__ == declared, (
        f"lbrain.__version__ is {lbrain.__version__!r} but pyproject declares "
        f"{declared!r} — `lbrain --version` would misreport the build"
    )


def test_cli_version_option_reports_it():
    """The bump must reach the surface a user actually reads.

    Asserting on `__version__` alone would pass if `--version` were wired to a
    different source — the check has to end where the user's eyes do.
    """
    from click.testing import CliRunner

    from lbrain.cli import main

    res = CliRunner().invoke(main, ["--version"])
    assert res.exit_code == 0, res.output
    assert lbrain.__version__ in res.output
