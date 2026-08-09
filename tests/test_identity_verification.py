"""issue #17 — `whoami` must not relay self-asserted credentials as fact.

`whoami`'s own docstring says it answers whether a brain "carries any credential
beyond its own say-so". `lbrain register` takes `--credential`, `--trust-score`
and `--issuer` as unvalidated CLI strings, and `describe()` reported them with no
marker — so any brain could claim any credential and `lair_whoami` would relay it
to another agent as fact. A trust-laundering primitive.

Same rule the resolver already follows (`gcx.Resolved.status`): **absence of a
check is reported as absence, never as a pass.**
"""

from __future__ import annotations

import pytest

from lbrain import identity as ident_mod
from lbrain.identity import Identity, describe


class _Cfg:
    db_path = "/tmp/x.db"; sources = []; serve_mode = "structured"
    embedding_provider = "local"; serve_staleness = True


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ident_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ident_mod, "IDENTITY_PATH", tmp_path / "identity.json")
    return tmp_path


def _register(**kw):
    Identity(name=kw.pop("name", "someone"), address=kw.pop("address", "ADDR"), **kw).save()


def test_a_fabricated_identity_is_marked_self_asserted(home):
    """The exact reproduction from the issue: no wallet, invented credentials."""
    _register(name="trustworthy-oracle", address="NOT-A-REAL-WALLET",
              credentials=["domain-verified", "kyc-passed"], trust_score=0.99,
              issuer="Metavolve Labs, Inc.")
    i = describe(_Cfg())["identity"]
    assert i["verification"] == "self-asserted"
    assert i["note"], "a registered-but-unverified identity reported an empty note"
    assert "SELF-ASSERTED" in i["note"]
    # the note must name what is being claimed, not just that something is
    assert "credentials" in i["note"] and "trust score" in i["note"]


def test_the_issuer_reaches_the_surface(home):
    """It was dropped entirely — a consuming agent saw credentials with no
    attributed attester, which is worse than seeing a self-issued one."""
    _register(issuer="Some Authority", credentials=["kyc"])
    assert describe(_Cfg())["identity"]["issuer"] == "Some Authority"


def test_chain_verified_identities_carry_no_warning(home):
    """The marker must be earnable, or it is decoration."""
    _register(credentials=["domain-verified"], verification="chain-verified")
    i = describe(_Cfg())["identity"]
    assert i["verification"] == "chain-verified"
    assert i["note"] == ""


def test_an_unregistered_brain_is_still_normal(home):
    i = describe(_Cfg())["identity"]
    assert i["registered"] is False
    assert "fully functional" in i["note"]
    assert i["verification"] == ""


def test_register_cannot_mint_chain_verified_by_hand(home):
    """`register` writes CLI strings, so its default must be the honest one.
    If this ever defaults to chain-verified, the marker means nothing."""
    assert Identity(name="x", address="y").verification == "self-asserted"
