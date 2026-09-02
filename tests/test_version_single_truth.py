"""VD-01 (2026-09-01, CSO fresh-venv verify of 0.1.9): the package shipped with
TWO version declarations and one was bumped — the artifact installed as 0.1.9
and self-reported 0.1.8, failing the release bar and poisoning every
version-keyed support flow. Two declarations is the defect; this gate makes
drift impossible to ship.

Why not importlib.metadata as the single source: on THIS box's editable install
the stamped metadata says 0.1.0 (stamped once, at install — the session-start
hook exists because of that lie), so metadata-derived __version__ would be
WRONG in dev exactly when developers read it. A literal + this equality gate
keeps both surfaces honest.
"""
import re
from pathlib import Path

import lbrain


def test_pyproject_and_dunder_version_agree():
    root = Path(lbrain.__file__).resolve().parent.parent
    py = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', py, re.M)
    assert m, "pyproject.toml has no version line"
    assert m.group(1) == lbrain.__version__, (
        f"version drift: pyproject={m.group(1)!r} vs lbrain.__version__="
        f"{lbrain.__version__!r} — bump BOTH or ship a self-misreporting artifact (VD-01)")
