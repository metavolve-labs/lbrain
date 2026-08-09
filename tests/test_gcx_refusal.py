"""G2 — refusal is a property of the RESOLVER, not a habit of its callers.

`resolve()` used to return payload bytes in every state and leave refusal to
whoever happened to be calling. Exactly one caller enforced it (the MCP
resource). The CLI wrote `--out` and printed `--quiet` BEFORE checking, so

    lbrain resolve gcx://x --quiet > file

captured unverified bytes and reported the failure afterwards — by which point
the shell already had the content. The exit code was correct and useless.

These are outcome tests: they assert the bytes are unreachable, not that a
particular function was called.
"""

from __future__ import annotations

import hashlib

import pytest
from click.testing import CliRunner

from lbrain import gcx
from lbrain.cli import main


def _node(txid, tags):
    return {"id": txid, "tags": [{"name": k, "value": v} for k, v in tags.items()]}


def _edges(*nodes):
    return {"data": {"transactions": {"edges": [{"node": n} for n in nodes]}}}


PAYLOAD = b"RFC: 793\nTransmission Control Protocol\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _wire(monkeypatch, *, recorded_hash, served: bytes):
    """A gateway that records `recorded_hash` on-chain and serves `served`."""
    monkeypatch.setattr(
        gcx, "_post_json",
        lambda *a, **k: _edges(_node("TX1", {"Canonical-SHA256": recorded_hash})))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: served)


# --- the library ------------------------------------------------------------

def test_content_is_unreachable_when_the_hash_does_not_match(monkeypatch):
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD + b"tampered")
    r = gcx.resolve("gcx://rfc/793")
    assert r.status == "HASH MISMATCH"
    with pytest.raises(gcx.ResolveError) as e:
        _ = r.content
    assert "refusing" in str(e.value).lower()


def test_content_is_unreachable_when_no_hash_was_recorded(monkeypatch):
    """Absence must not be a pass — including for byte access."""
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges(_node("TX1", {})))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/2616")
    assert r.status.startswith("UNVERIFIABLE")
    with pytest.raises(gcx.ResolveError):
        _ = r.content


def test_content_is_returned_when_it_verifies(monkeypatch):
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.verified and r.content == PAYLOAD


def test_raw_content_is_the_deliberate_escape_hatch(monkeypatch):
    """Unverified bytes remain reachable — but only by naming them, so the
    decision is greppable in review rather than implicit in a field access."""
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD + b"tampered")
    r = gcx.resolve("gcx://rfc/793")
    assert not r.verified
    assert r.raw_content == PAYLOAD + b"tampered"


# --- the CLI, which is where the real leak was ------------------------------

def test_cli_out_writes_nothing_when_verification_fails(monkeypatch, tmp_path):
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD + b"tampered")
    dest = tmp_path / "out.txt"
    res = CliRunner().invoke(main, ["resolve", "gcx://rfc/793", "--out", str(dest)])
    assert res.exit_code == 1
    assert not dest.exists(), "unverified bytes were written to disk"


def test_cli_quiet_emits_nothing_when_verification_fails(monkeypatch):
    """The original bug: bytes on stdout, failure reported after — a shell
    redirect has the content regardless of the exit code."""
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD + b"tampered")
    res = CliRunner().invoke(main, ["resolve", "gcx://rfc/793", "--quiet"])
    assert res.exit_code == 1
    assert b"Transmission Control Protocol" not in res.stdout_bytes


def test_cli_quiet_emits_content_when_it_verifies(monkeypatch):
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD)
    res = CliRunner().invoke(main, ["resolve", "gcx://rfc/793", "--quiet"])
    assert res.exit_code == 0
    assert "Transmission Control Protocol" in res.output


def test_cli_report_withholds_a_preview_of_unverified_bytes(monkeypatch):
    """A four-line excerpt is still content, and a reader who was just told
    HASH MISMATCH will read it anyway. Report the size; withhold the substance."""
    _wire(monkeypatch, recorded_hash=SHA, served=PAYLOAD + b"tampered")
    res = CliRunner().invoke(main, ["resolve", "gcx://rfc/793"])
    assert res.exit_code == 1
    assert "HASH MISMATCH" in res.output
    assert "preview withheld" in res.output
    assert "Transmission Control Protocol" not in res.output
