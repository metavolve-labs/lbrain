"""gcx:// resolution — offline. No network, no gateway, no keys.

The property under test is WHERE THE HASH COMES FROM: an on-chain tag, not a
value this package ships. A stranger must be able to verify without trusting us.
"""

from __future__ import annotations

import hashlib

import pytest

from lbrain import gcx


def _node(txid, tags):
    return {"id": txid, "tags": [{"name": k, "value": v} for k, v in tags.items()]}


def _edges(*nodes):
    return {"data": {"transactions": {"edges": [{"node": n} for n in nodes]}}}


PAYLOAD = b"RFC: 793\nTransmission Control Protocol\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def test_parse_accepts_both_registered_schemes():
    assert gcx.parse("gcx://rfc/793") == ("gcx", "rfc/793")
    assert gcx.parse("aet://works/0020") == ("aet", "works/0020")


@pytest.mark.parametrize("bad", [
    "not-a-name", "http://example.com/x", "gcx:/rfc/793", "gcx://", "ftp://rfc/793",
])
def test_parse_rejects_anything_else(bad):
    with pytest.raises(gcx.ResolveError):
        gcx.parse(bad)


def test_resolve_verifies_against_the_on_chain_hash(monkeypatch):
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges(
        _node("TX1", {"Type": "rfc-fulltext", "Canonical-SHA256": SHA})))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)

    r = gcx.resolve("gcx://rfc/793")
    assert r.verified and r.status == "VERIFIED"
    assert r.expected_sha256 == SHA and r.actual_sha256 == SHA


def test_tampered_content_fails_loudly(monkeypatch):
    """The whole point. Bytes that do not match the chain must not pass."""
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges(
        _node("TX1", {"Canonical-SHA256": SHA})))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD + b"tampered")

    r = gcx.resolve("gcx://rfc/793")
    assert not r.verified
    assert r.status == "HASH MISMATCH"


def test_absent_hash_is_reported_as_absent_never_as_a_pass(monkeypatch):
    """~15 of 9,806 first-batch records carry no Canonical-SHA256. Unverifiable
    must never render as verified."""
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges(_node("TX1", {})))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)

    r = gcx.resolve("gcx://rfc/2616")
    assert not r.verified
    assert "UNVERIFIABLE" in r.status


def test_sidecar_is_skipped_in_favour_of_the_payload(monkeypatch):
    """A name legitimately covers payload + JSON metadata sidecar. Selection is
    by semantics (`Fulltext-Tx` present / `Canonical-SHA256` present), not by a
    name guess — an earlier filter keyed on Type endswith 'sidecar' and missed,
    because the real sidecar is typed `GCX-PAPR-H`."""
    sidecar = _node("SIDECAR", {"Type": "GCX-PAPR-H", "Fulltext-Tx": "TX1",
                                "Content-Type": "application/json"})
    payload = _node("TX1", {"Type": "rfc-fulltext", "Canonical-SHA256": SHA})
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges(sidecar, payload))
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)

    r = gcx.resolve("gcx://rfc/793")
    assert r.txid == "TX1" and r.verified


def test_an_ambiguous_name_refuses_rather_than_guessing(monkeypatch):
    """Two payloads claiming one name must not silently resolve to the first."""
    a = _node("TXA", {"Canonical-SHA256": SHA})
    b = _node("TXB", {"Canonical-SHA256": SHA})
    monkeypatch.setattr(gcx, "_post_json", lambda *a_, **k: _edges(a, b))

    with pytest.raises(gcx.ResolveError, match="refusing to guess"):
        gcx.resolve("gcx://rfc/793")


def test_unknown_name_raises(monkeypatch):
    monkeypatch.setattr(gcx, "_post_json", lambda *a, **k: _edges())
    with pytest.raises(gcx.ResolveError, match="not registered"):
        gcx.resolve("gcx://rfc/999999")


def test_gateway_is_overridable(monkeypatch):
    """Enterprises must be able to point at their own gateway — no hard
    dependency on arweave.net or on us."""
    seen = {}
    monkeypatch.setattr(gcx, "_post_json", lambda url, *a, **k: (
        seen.__setitem__("graphql", url),
        _edges(_node("TX1", {"Canonical-SHA256": SHA})))[1])
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: (
        seen.__setitem__("gateway", k.get("gateway")), PAYLOAD)[1])

    gcx.resolve("gcx://rfc/793", gateway="https://my.gw", graphql="https://my.gql")
    assert seen["graphql"] == "https://my.gql"
    assert seen["gateway"] == "https://my.gw"
