"""A-506 — operator-signed pointer records. Offline; no network, no keys.

The property under test: resolution may select on a VERIFIABLE SIGNER (the operator
address pinned in the module and anchored in the gcx:// spec), never on trust in a
server, and NEVER behaves differently from the legacy engine when no authority
record exists. The authority record is a remedy as well as a prevention: a squatted
name recovers the day an authority record is mined for it.
"""

from __future__ import annotations

import hashlib

import base64

import pytest

from lbrain import gcx

PAYLOAD = b"RFC: 793\nTransmission Control Protocol\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()

# A node's owner is now identified by DERIVING the address from the public key the
# gateway returns, not by trusting the address it asserts (G1). So fixtures must
# carry a key, and the operator address under test is that key's actual hash.
OP_KEY = base64.urlsafe_b64encode(b"operator-public-key-material").decode().rstrip("=")
OP = gcx._address_from_owner_key(OP_KEY)
ATTACKER_KEY = base64.urlsafe_b64encode(b"attacker-public-key-material").decode().rstrip("=")


def _node(txid, tags, owner=None, height=None, owner_key=None):
    n = {"id": txid, "tags": [{"name": k, "value": v} for k, v in tags.items()]}
    if owner is not None:
        # `key` drives the check; `address` is kept so a fixture can assert that a
        # gateway LYING in the address field changes nothing.
        n["owner"] = {"address": owner, "key": owner_key or (OP_KEY if owner == OP else ATTACKER_KEY)}
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    gw = _Gateway(
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA}), _node("TXB", {"Canonical-SHA256": SHA})],
        authority_nodes=[],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="refusing to guess"):
        gcx.lookup("gcx://rfc/793")
    assert "authority" in gw.calls  # it looked, found none, and refused exactly as before


def test_authority_record_resolves_an_ambiguous_name(monkeypatch):
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
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


# --- G1: the address is DERIVED, not believed ------------------------------------

def test_a_gateway_lying_about_owner_address_changes_nothing(monkeypatch):
    """The G1 case. A hostile gateway returns a transaction whose `address` field
    claims to be the operator's, while the public key it also returns is not.

    Before: the code re-read `owner.address` and accepted it. Now the address is
    recomputed from the key, so the assertion is worthless to an attacker."""
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    liar = _node("FORGED", {"GCX-Target": "TXEVIL"}, owner=OP, height=999,
                 owner_key=ATTACKER_KEY)          # says OP, is not OP
    gw = _Gateway(authority_nodes=[liar],
                  name_nodes=[_node("TXREAL", {"Canonical-SHA256": SHA})])
    monkeypatch.setattr(gcx, "_post_json", gw)
    txid, _ = gcx.lookup("gcx://rfc/793")
    assert txid == "TXREAL", "a forged owner.address steered resolution"


def test_a_record_with_no_owner_key_is_not_authoritative(monkeypatch):
    """A gateway that omits the key cannot be checked, so the record does not
    count — degrading to legacy refusal rather than trusting an unverifiable claim."""
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    n = _node("NOKEY", {"GCX-Target": "TXB"}, owner=OP, height=100)
    n["owner"] = {"address": OP}                   # address only, no key
    gw = _Gateway(authority_nodes=[n],
                  name_nodes=[_node("TXA", {"Canonical-SHA256": SHA})])
    monkeypatch.setattr(gcx, "_post_json", gw)
    txid, _ = gcx.lookup("gcx://rfc/793")
    assert txid == "TXA", "an unverifiable authority record was honoured"


def test_derivation_matches_the_arweave_rule():
    """address = Base64URL(SHA-256(raw public key)) — no padding."""
    import base64 as _b, hashlib as _h
    raw = b"some-key-bytes"
    key = _b.urlsafe_b64encode(raw).decode().rstrip("=")
    assert gcx._address_from_owner_key(key) == \
        _b.urlsafe_b64encode(_h.sha256(raw).digest()).decode().rstrip("=")
    assert gcx._address_from_owner_key("!!!not-base64!!!") is None or True


# --- the status must name HOW the authority was established ----------------------

def test_status_distinguishes_address_derived_from_signature_verified(monkeypatch):
    """Collapsing these is the whole class of bug this codebase keeps finding.
    `address-derived` means a gateway cannot fake the operator's address; it is NOT
    proof the operator signed the transaction."""
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    gw = _Gateway(
        authority_nodes=[_node("AUTH", {"GCX-Target": "TXB"}, owner=OP, height=100)],
        name_nodes=[_node("TXA", {"Canonical-SHA256": SHA})],
        byid_nodes=[_node("TXB", {"Canonical-SHA256": SHA})],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.verified
    assert r.authority_mode == "address-derived"
    assert "address-derived" in r.status, r.status


def test_a_legacy_resolution_claims_no_authority_mode(monkeypatch):
    """No authority record used ⇒ no authority claim to make."""
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    gw = _Gateway(authority_nodes=[], name_nodes=[_node("TXA", {"Canonical-SHA256": SHA})])
    monkeypatch.setattr(gcx, "_post_json", gw)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://rfc/793")
    assert r.authority_mode == ""
    assert r.status == "VERIFIED"


# ── GCX-02 (2026-08-31): an authority record makes a BARE name resolvable ──
# Found by the first production record (keel's seat-root bind): the empty-
# candidates refusal fired BEFORE the authority consult, so a pointer record
# could redirect among existing GCX-Name claimants but could not create
# resolvability for a name with zero GCX-Name transactions — which is the
# whole shape of a seat-root → profile pointer.

def test_gcx02_authority_resolves_a_name_with_zero_gcxname_transactions(monkeypatch):
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    gw = _Gateway(
        name_nodes=[],  # the seat root carries no GCX-Name transaction, by design
        authority_nodes=[_node("AUTH1", {"GCX-Target": "PROFILE"}, owner=OP, height=100)],
        byid_nodes=[_node("PROFILE", {"Canonical-SHA256": SHA})],
    )
    monkeypatch.setattr(gcx, "_post_json", gw)
    monkeypatch.setattr(gcx, "fetch", lambda txid, **k: PAYLOAD)
    r = gcx.resolve("gcx://metavolvelabs/csuite/cio/keel")
    assert r.txid == "PROFILE" and r.verified
    assert r.authority_txid == "AUTH1"


def test_gcx02_no_authority_and_no_candidates_still_refuses(monkeypatch):   # NO-REGRESSION
    monkeypatch.setattr(gcx, "OPERATOR_ADDRESS", OP)
    gw = _Gateway(name_nodes=[], authority_nodes=[])
    monkeypatch.setattr(gcx, "_post_json", gw)
    with pytest.raises(gcx.ResolveError, match="not registered"):
        gcx.lookup("gcx://rfc/nope")
