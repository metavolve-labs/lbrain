"""CCO bug 2026-08-26: doctor must report build provenance + dirty/editable risk.

A running CLI backed by an editable, concurrently-edited checkout can serve a
torn read. `doctor --json` now reports where the code came from and whether that
checkout is dirty, so the risk is named instead of surfacing as a NameError.
"""
import json
from click.testing import CliRunner

from lbrain.cli import main, _build_provenance


def test_provenance_shape():
    p = _build_provenance()
    assert set(p) >= {"version", "package_path", "editable_checkout", "commit", "dirty"}
    assert isinstance(p["editable_checkout"], bool)
    assert p["package_path"]


def test_doctor_json_includes_build_block():
    r = CliRunner().invoke(main, ["doctor", "--json"])
    # doctor may exit non-zero on embedding drift; the block must still be present
    payload = json.loads(r.output)
    assert "build" in payload
    assert "editable_checkout" in payload["build"]
    assert "dirty" in payload["build"]


def test_dirty_editable_is_reported_when_in_git_checkout():
    p = _build_provenance()
    if p["editable_checkout"]:
        # in this dev tree, commit is known and dirty is a real bool (not None)
        assert p["commit"] is not None
        assert isinstance(p["dirty"], bool)
