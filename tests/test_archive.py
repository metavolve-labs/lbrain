"""Tier-2 archive — crypto, round-trip, snapshot, crypto-shred, and the index loop.

These exercise the handoff's Definition of Done with the offline LocalTransport: encrypt →
archive → index snapshot → fetch-by-txid → decrypt → byte-identical → crypto-shred ⇒
undecryptable. No wallet, no network, no embedding API required.
"""

from __future__ import annotations

import os

import pytest

from lbrain import crypto
from lbrain.archive import Archiver, LocalTransport, _content_txid, make_snapshot, verify_on_chain
from lbrain.crypto import CryptoError, Keystore
from lbrain.store import Store

PASS = "correct horse battery staple"


# --------------------------------------------------------------------------- on-chain verify


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_verify_on_chain_settled(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _FakeResp(200, {"number_of_confirmations": 345, "block_height": 1932366}),
    )
    v = verify_on_chain("any-txid")
    assert v["settled"] is True and v["confirmations"] == 345 and v["block_height"] == 1932366


def test_verify_on_chain_ghost_is_not_settled(monkeypatch):
    """A local-only ghost txid (gateway 404) must report settled=False — the whole
    point of bypassing the local-first mirror."""
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(404))
    v = verify_on_chain("ghost-txid")
    assert v["settled"] is False and v["error"] == "not found" and v["http"] == 404


def test_verify_on_chain_pending(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(202))
    v = verify_on_chain("pending-txid")
    assert v["settled"] is False and v["error"] == "pending"


# --------------------------------------------------------------------------- crypto


def test_encrypt_decrypt_roundtrip():
    pt = b"the full session bytes, verbatim and immutable \x00\x01\x02 \xf0\x9f\xa7\xa0"
    payload_env, key_env = crypto.encrypt(pt, PASS)
    assert payload_env.startswith(crypto.PAYLOAD_MAGIC)
    assert key_env.startswith(crypto.KEY_MAGIC)
    # Ciphertext must not leak plaintext, and the key must NOT travel in the payload envelope.
    assert b"session" not in payload_env
    assert crypto.decrypt(payload_env, key_env, PASS) == pt


def test_wrong_passphrase_fails():
    payload_env, key_env = crypto.encrypt(b"secret", PASS)
    with pytest.raises(CryptoError):
        crypto.decrypt(payload_env, key_env, "wrong passphrase")


def test_tamper_detected():
    payload_env, key_env = crypto.encrypt(b"secret payload here", PASS)
    tampered = bytearray(payload_env)
    tampered[-1] ^= 0x01  # flip a ciphertext bit
    with pytest.raises(CryptoError):
        crypto.decrypt(bytes(tampered), key_env, PASS)


def test_each_archive_uses_a_distinct_key():
    # Distinct random DEK + salt per call → envelopes differ even for identical plaintext.
    a1, k1 = crypto.encrypt(b"same", PASS)
    a2, k2 = crypto.encrypt(b"same", PASS)
    assert a1 != a2 and k1 != k2


def test_keystore_shred(tmp_path):
    ks = Keystore(tmp_path / "keys")
    _, key_env = crypto.encrypt(b"x", PASS)
    ks.put("tx123", key_env)
    assert ks.has("tx123")
    assert ks.get("tx123") == key_env
    assert ks.shred("tx123") is True
    assert not ks.has("tx123")
    assert ks.shred("tx123") is False  # already gone


# --------------------------------------------------------------------------- snapshot


def test_extractive_snapshot_is_faithful_and_smaller():
    class Cfg:  # no API key → forces the deterministic extractive path
        embedding_provider = "gemini"
        gemini_api_key = ""
        openai_api_key = ""

    text = (
        "# Decision\nWe locked store-full-read-snapshot on 2026-06-03.\n\n"
        "Some prose paragraph explaining the rationale at length about asymmetry of cost.\n\n"
        "## Open\n- verify byte-identical round-trip\n- crypto-shred check\n"
    ) * 4
    snap = make_snapshot(text, Cfg())
    assert snap and len(snap) < len(text)
    assert "2026-06-03" in snap  # preserves a specific fact
    assert "#" in snap           # preserves structure


# --------------------------------------------------------------------------- full loop


class _FakeEmbedder:
    """Deterministic stand-in for the embedding API (offline)."""

    def __init__(self, dim=8):
        self.dim = dim

    def embed_one(self, text: str) -> bytes:
        import hashlib
        import struct

        h = hashlib.sha256(text.encode()).digest()
        vals = [((h[i % len(h)] / 255.0) - 0.5) for i in range(self.dim)]
        norm = sum(v * v for v in vals) ** 0.5 or 1.0
        return struct.pack(f"<{self.dim}f", *[v / norm for v in vals])


def _cfg(tmp_path, monkeypatch):
    # Point LBRAIN_HOME at a temp dir so config + keystore + local archive are isolated.
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "lbrain_home"))
    import importlib

    from lbrain import config as config_mod
    importlib.reload(config_mod)
    cfg = config_mod.Config()
    cfg.embedding_dim = 8
    cfg.embedding_provider = "gemini"
    cfg.gemini_api_key = ""
    cfg.openai_api_key = ""
    cfg.arweave_enabled = False
    cfg.arweave_transport = "local"
    cfg.archive_namespace = "private"
    return cfg, config_mod


def test_archive_retrieve_byte_identical(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "brain.db", embedding_dim=cfg.embedding_dim)
    emb = _FakeEmbedder(cfg.embedding_dim)
    arc = Archiver(cfg, store, emb)

    payload = b"# Session\nWe decided X. Tad approved Y on 2026-06-03. Numbers: 42, 1337.\n" * 20
    res = arc.archive(payload, title="test session", passphrase=PASS)

    assert res.txid and res.n_bytes == len(payload)
    # Byte-identical round-trip (the core DoD).
    assert arc.retrieve(res.txid, PASS) == payload
    # Indexed snapshot is semantically recallable.
    hits = store.search_archives(emb.embed_one("what did Tad approve"), k=3)
    assert any(h["txid"] == res.txid for h in hits)
    store.close()


def test_crypto_shred_makes_record_undecryptable(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "brain.db", embedding_dim=cfg.embedding_dim)
    arc = Archiver(cfg, store, _FakeEmbedder(cfg.embedding_dim))

    payload = b"sensitive permanent record"
    res = arc.archive(payload, title="to be shredded", passphrase=PASS)
    assert arc.retrieve(res.txid, PASS) == payload  # decryptable before shred

    assert arc.shred(res.txid) is True  # HARD shred (default)
    # The permanent ciphertext is still on the transport...
    assert arc.transport.get(res.txid)  # blob persists (permanence)
    # ...but without the key it is unrecoverable (logical delete).
    with pytest.raises(CryptoError):
        arc.retrieve(res.txid, PASS)
    # Hard shred ALSO erases the local cleartext snapshot + its FTS/vector rows —
    # nothing readable about the record survives locally, only an audit stub.
    row = store.get_archive(res.txid)
    assert row["shredded"] == 1
    assert row["snapshot"] == ""
    fts = store.db.execute(
        "SELECT COUNT(*) AS n FROM fts_archives WHERE rowid = ?", (row["archive_id"],)
    ).fetchone()["n"]
    vec = store.db.execute(
        "SELECT COUNT(*) AS n FROM vec_archives WHERE rowid = ?", (row["archive_id"],)
    ).fetchone()["n"]
    assert fts == 0 and vec == 0
    store.close()


def test_soft_shred_keeps_snapshot(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "brain.db", embedding_dim=cfg.embedding_dim)
    arc = Archiver(cfg, store, _FakeEmbedder(cfg.embedding_dim))
    res = arc.archive(b"keep my snapshot for browsing", title="soft", passphrase=PASS)

    arc.shred(res.txid, purge_snapshot=False)  # soft
    row = store.get_archive(res.txid)
    assert row["shredded"] == 1
    assert row["snapshot"]  # snapshot retained for browsing
    # Payload still unrecoverable (key was destroyed regardless of soft/hard).
    with pytest.raises(CryptoError):
        arc.retrieve(res.txid, PASS)
    store.close()


def test_capture_is_idempotent(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, monkeypatch)
    store = Store(tmp_path / "brain.db", embedding_dim=cfg.embedding_dim)
    arc = Archiver(cfg, store, _FakeEmbedder(cfg.embedding_dim))
    payload = b"# Session\nidempotent capture content, fired twice\n"

    r1 = arc.archive(payload, title="s", passphrase=PASS, skip_if_exists=True)
    r2 = arc.archive(payload, title="s", passphrase=PASS, skip_if_exists=True)
    assert r1.skipped is False and r2.skipped is True
    assert r2.txid == r1.txid and r2.source_hash == r1.source_hash
    # No duplicate row — content dedup held despite non-deterministic ciphertext.
    n = store.db.execute("SELECT COUNT(*) AS n FROM archives").fetchone()["n"]
    assert n == 1
    store.close()


def test_transport_override_forces_local(tmp_path, monkeypatch):
    cfg, _ = _cfg(tmp_path, monkeypatch)
    cfg.arweave_enabled = True          # global config would pick the Arweave transport…
    cfg.arweave_transport = "arweave"
    store = Store(tmp_path / "brain.db", embedding_dim=cfg.embedding_dim)
    t = LocalTransport(tmp_path / "arch")
    arc = Archiver(cfg, store, _FakeEmbedder(cfg.embedding_dim), transport=t)
    assert arc.transport.name == "local"   # …but the explicit override wins (hook/capture path)
    res = arc.archive(b"x", title="s", passphrase=PASS)
    assert res.transport == "local"
    store.close()


def test_force_extractive_skips_llm():
    class Cfg:  # a key is present, but force_extractive must NOT call the network
        embedding_provider = "gemini"
        gemini_api_key = "FAKE-KEY-WOULD-FAIL-IF-CALLED"
        openai_api_key = ""

    snap = make_snapshot("# Heading\nlead line with fact 2026-06-03.\n", Cfg(), force_extractive=True)
    assert "2026-06-03" in snap  # deterministic extractive, no API call


def test_local_transport_is_content_addressed(tmp_path):
    t = LocalTransport(tmp_path / "archive")
    data = b"deterministic blob"
    txid = t.put(data, {"App-Name": "LBrain"})
    assert txid == _content_txid(data)
    assert t.get(txid) == data
