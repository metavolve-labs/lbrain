"""The runtime fingerprint must never write a credential carried inside GEMINI_BASE_URL.

#89 recorded GEMINI_BASE_URL BY VALUE in build-manifest.json (`runtime_inputs.env_values`).
A base URL can carry a credential in its query (`?key=...`) or its userinfo
(`https://user:<pass>@host`), and the manifest sits on disk beside every epoch (CSO round 3
on #89, 2026-10-18T0119Z). The manifest now stores a display form with the userinfo, query
and fragment removed, plus a sha256 of the full value, which is what the equality check
compares. The raw value is never written.
"""
from __future__ import annotations

from pathlib import Path

from lbrain import epoch_build

from test_epoch_wait_for_builder import _mk_home

SECRET_Q = "qparam" + "SECRET" + "7781"
SECRET_U = "userinfo" + "SECRET" + "5512"


def _all_bytes_under(root: Path) -> bytes:
    out = b""
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                out += p.read_bytes()
            except OSError:
                pass
    return out


def test_a_query_key_in_the_base_url_never_lands_on_disk(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", f"https://gw.example.invalid/v1?key={SECRET_Q}&alt=json")
    eid = epoch_build.build(home, cfg)["epoch_id"]
    assert SECRET_Q.encode() not in _all_bytes_under(home)
    import json
    man = json.loads((home / "epochs" / eid / "build-manifest.json").read_text())
    shown = man["runtime_inputs"]["env_values"]["GEMINI_BASE_URL"]
    assert shown["display"] == "https://gw.example.invalid/v1", shown
    assert len(shown["sha256"]) == 64


def test_userinfo_in_the_base_url_never_lands_on_disk(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", f"https://svc:{SECRET_U}@gw.example.invalid:8443/v1")
    epoch_build.build(home, cfg)
    blob = _all_bytes_under(home)
    assert SECRET_U.encode() not in blob
    assert b"svc:" not in blob                     # the user name goes with the userinfo
    assert b"gw.example.invalid:8443/v1" in blob   # host, port and path stay readable


def test_equality_still_detects_a_change_hidden_from_the_display(tmp_path, monkeypatch):
    """Two URLs that differ ONLY in the stripped part display the same but must not fingerprint
    the same: a different key can route to a different deployment."""
    home, _ = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gw.example.invalid/v1?key=A1")
    a = epoch_build.runtime_fingerprint(home)
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gw.example.invalid/v1?key=B2")
    b = epoch_build.runtime_fingerprint(home)
    monkeypatch.setenv("GEMINI_BASE_URL", "https://other.example.invalid/v1?key=A1")
    c = epoch_build.runtime_fingerprint(home)
    assert len({a, b, c}) == 3


def test_equality_holds_for_the_same_value(tmp_path, monkeypatch):
    home, _ = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gw.example.invalid/v1?key=A1")
    assert epoch_build.runtime_fingerprint(home) == epoch_build.runtime_fingerprint(home)


def test_a_plain_base_url_is_shown_as_is(tmp_path, monkeypatch):
    """Normal path: nothing to strip, the display is the value."""
    home, _ = _mk_home(tmp_path)
    url = "https://generativelanguage.googleapis.com/v1beta"
    monkeypatch.setenv("GEMINI_BASE_URL", url)
    shown = epoch_build._runtime_inputs(home)["env_values"]["GEMINI_BASE_URL"]
    assert shown["display"] == url


def test_an_unset_base_url_is_recorded_as_empty(tmp_path, monkeypatch):
    home, _ = _mk_home(tmp_path)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    shown = epoch_build._runtime_inputs(home)["env_values"]["GEMINI_BASE_URL"]
    assert shown == {"display": "", "sha256": ""}


def test_a_value_that_is_not_a_url_is_never_shown(tmp_path, monkeypatch):
    """Without a scheme and host there is nothing safe to display: hash only."""
    home, _ = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", "not a url " + SECRET_Q)
    shown = epoch_build._runtime_inputs(home)["env_values"]["GEMINI_BASE_URL"]
    assert SECRET_Q not in shown["display"] and len(shown["sha256"]) == 64
