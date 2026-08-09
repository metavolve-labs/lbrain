"""A-506 — operator-signed pointer records. Offline; no network, no keys.

The property under test: resolution may select on a VERIFIABLE SIGNER (the operator
address pinned in the module and anchored in the gcx:// spec), never on trust in a
server, and NEVER behaves differently from the legacy engine when no authority
record exists. The authority record is a remedy as well as a prevention: a squatted
name recovers the day an authority record is mined for it.
"""

from __future__ import annotations

import hashlib

import pytest

from lbrain import gcx

PAYLOAD = b"RFC: 793\nTransmission Control Protocol\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()

OP = gcx.OPERATOR_ADDRESS


def _node(txid, tags, owner=None, height=None):
    n = {"id": txid, "tags": [{"name": k, "value": v} for k, v in tags.items()]}
    if owner is not None:
        n["owner"] = {"address": owner}
    n["block"] = {"height": height} if height is not None else None
    return n


def _edges(*nodes):
    return {"data": {"transactions": {"edges": [{"node": n} for n in nodes]}}}


class _Gateway:
    """Dispatch mock: answers the name query, the authority query and the by-id
    query differently, the way the real gateway does."""

    def __init__(self, name_nodes, authority_nodes=(), byid_nodes=(), authority_raises=False):
        self.name_nodes = name_nodes
        self.authority_nodes = authority_nodes
        self.byid_nodes = byid_nodes
        self.authority_raises = authority_raises
        self.calls = []

    def __call__(self, url, payload, timeout):
        q = payload["query"]
        if "GCX-Authority" in q:
            self.calls.append("authority")
            if self.authority_raises:
                raise OSError("gateway down")
            return _edges(*self.authority_nodes)
        if "ids:" in q or "$ids" in q:
            self.calls.append("byid")
            return _edges(*self.byid_nodes)
        self.calls.append("name")
        return _edges(*self.name_nodes)


def test_no_authority_record_means_legacy_behaviour_ambiguity_still_refuses(monkeypatch):
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="refusing to guess"):
        gcx.lookup("gcx://rfc/793")
    assert "authority" in gw.calls  # it looked, found none, and refused exactly as before


def test_authority_record_resolves_an_ambiguous_name(monkeypatch):
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[_node("AUTH1", {"GCX-Target": "TXB"}, owner=OP, height=100)],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.txid == "TXB" and r.verified
    assert r.authority_txid == "AUTH1"  # provenance: WHICH record selected this


def test_a_stranger_cannot_mint_authority_wrong_owner_is_ignored(monkeypatch):
    """The gateway filter is not trusted: even if a non-operator record comes back,
    the returned owner is re-verified and the record ignored."""
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[_node("EVIL", {"GCX-Target": "TXA"}, owner="ATTACKER", height=999)],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="refusing to guess"):
        gcx.lookup("gcx://rfc/793")


def test_supersession_latest_by_block_height_wins(monkeypatch):
    """Two corrections from the SAME operator key are a legitimate state — the rule
    is defined before ship: latest-by-block-height."""
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[
            _node("AUTH-OLD", {"GCX-Target": "TXA"}, owner=OP, height=100),
            _node("AUTH-NEW", {"GCX-Target": "TXB"}, owner=OP, height=200),
        ],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    txid, _tags = gcx.lookup("gcx://rfc/793")
    assert txid == "TXB"


def test_unconfirmed_loses_to_confirmed(monkeypatch):
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[
            _node("AUTH-MINED", {"GCX-Target": "TXA"}, owner=OP, height=100),
            _node("AUTH-PENDING", {"GCX-Target": "TXB"}, owner=OP, height=None),
        ],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    txid, _tags = gcx.lookup("gcx://rfc/793")
    assert txid == "TXA"


def test_two_unconfirmed_authorities_refuse_they_cannot_be_ordered(monkeypatch):
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA})],
        authority_nodes=[
            _node("P1", {"GCX-Target": "TXA"}, owner=OP, height=None),
            _node("P2", {"GCX-Target": "TXB"}, owner=OP, height=None),
        ],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="cannot be ordered"):
        gcx.lookup("gcx://rfc/793")


def test_squat_remedy_authority_overrides_a_single_wrong_claimant(monkeypatch):
    """The remedy case: only a squatter's tx carries the GCX-Name tag; the authority
    record points at the true payload, which is fetched by id."""
    gw = _Gateway(
        name_nodes=[_node("SQUAT", {"Canonical-SHA256": "0" * 64})],
        authority_nodes=[_node("AUTH1", {"GCX-Target": "TRUE-TX"}, owner=OP, height=50)],
        byid_nodes=[_node("TRUE-TX", {"Canonical-SHA256": SHA})],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.txid == "TRUE-TX" and r.verified
    assert gw.calls == ["name", "authority", "byid"]


def test_dangling_authority_pointer_refuses(monkeypatch):
    gw = _Gateway(
        name_nodes=[_node("SQUAT", {"Canonical-SHA256": SHA})],
        authority_nodes=[_node("AUTH1", {"GCX-Target": "GONE"}, owner=OP, height=50)],
        byid_nodes=[],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="dangling pointer"):
        gcx.lookup("gcx://rfc/793")


def test_authority_gateway_failure_degrades_to_legacy_never_worse(monkeypatch):
    """A suppressed authority query re-creates yesterday's refusal — never a wrong
    answer. Single-claimant names still resolve."""
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_raises=True,
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="refusing to guess"):
        gcx.lookup("gcx://rfc/793")

    gw2 = _Gateway(name_nodes=[_node("TX1", {"Canonical-SHA256": SHA})], authority_raises=True)
    monkeypatch.setattr(gcx, "_post_json", gw2)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.txid == "TX1" and r.verified and r.authority_txid is None
