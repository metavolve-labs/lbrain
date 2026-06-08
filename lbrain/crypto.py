"""Tier-2 crypto — AES-256-GCM payloads, Argon2id-derived keys, per-item crypto-shred.

The one idea this serves: LBrain's Tier-2 archive is *verifiable* memory, written to a
permanent substrate (Arweave). Permanence is a problem for privacy — you cannot "delete" a
permaweb transaction. So we make the ciphertext permanent and the *key* deletable:

    DEK  (data-encryption key)  — random per session, encrypts the payload.
    KEK  (key-encryption key)   — derived from the user passphrase via Argon2id, wraps the DEK.

The wrapped DEK lives ONLY in a local keystore (``~/.lbrain/keys/<txid>.key``, chmod 600) —
NEVER on Arweave, NEVER in config.toml. Destroy that one file and the permanent ciphertext
is unrecoverable: **crypto-shred = logical delete** at single-item granularity, without
having to forget the master passphrase (which would shred everything at once).

Envelopes are self-describing and versioned so decryption needs only (passphrase, payload
envelope, key envelope) — the Argon2id parameters travel inside the key envelope, so a future
parameter bump never strands old archives.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

# --- envelope magics (versioned) --------------------------------------------
PAYLOAD_MAGIC = b"LBARC1"  # LBrain ARchive Ciphertext v1
KEY_MAGIC = b"LBARK1"      # LBrain ARchive Key (wrapped DEK) v1

# --- Argon2id parameters (interactive-but-strong defaults) ------------------
# RFC 9106 "second recommended" tier, comfortable for a local CLI. Stored in the
# key envelope, so these can be raised later without breaking existing archives.
ARGON2_TIME = 3            # iterations
ARGON2_MEMORY = 64 * 1024  # KiB → 64 MiB
ARGON2_LANES = 4           # parallelism
KEY_LEN = 32               # 256-bit keys (AES-256 / random DEK)
SALT_LEN = 16
NONCE_LEN = 12

# Hardening: the wrapped-DEK envelope is portable and backup-able, so its embedded
# KDF parameters are attacker-influenceable. Clamp them on the unwrap path so a
# tampered/corrupt key file can't request a multi-TB Argon2 allocation (local DoS / OOM).
ARGON2_MAX_TIME = 16
ARGON2_MAX_MEMORY = 2 * 1024 * 1024  # KiB → 2 GiB
ARGON2_MAX_LANES = 16


class CryptoError(Exception):
    """Raised when an envelope is malformed or decryption/authentication fails."""


def _derive_kek(passphrase: str, salt: bytes, *, time=ARGON2_TIME,
                memory=ARGON2_MEMORY, lanes=ARGON2_LANES) -> bytes:
    """Argon2id: passphrase + salt → 32-byte key-encryption key."""
    if not passphrase:
        raise CryptoError("empty passphrase — set LBRAIN_ARCHIVE_PASSPHRASE or pass one")
    kdf = Argon2id(
        salt=salt, length=KEY_LEN, iterations=time, lanes=lanes, memory_cost=memory
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _wrap_dek(dek: bytes, passphrase: str) -> bytes:
    """Wrap a DEK under a fresh passphrase-derived KEK → a self-describing key envelope.
    KEY_MAGIC | time(u32) | memory(u32) | lanes(u32) | salt_len(u8) | salt | knonce | wrapped."""
    salt = os.urandom(SALT_LEN)
    kek = _derive_kek(passphrase, salt)
    knonce = os.urandom(NONCE_LEN)
    wrapped = AESGCM(kek).encrypt(knonce, dek, KEY_MAGIC)
    return (
        KEY_MAGIC
        + struct.pack(">IIIB", ARGON2_TIME, ARGON2_MEMORY, ARGON2_LANES, SALT_LEN)
        + salt
        + knonce
        + wrapped
    )


def encrypt(plaintext: bytes, passphrase: str) -> tuple[bytes, bytes]:
    """Encrypt ``plaintext`` for the permaweb.

    Returns ``(payload_envelope, key_envelope)``:
      - ``payload_envelope`` is what gets written to Arweave — it contains the
        ciphertext but NOT the key, so the permaweb never sees anything decryptable.
      - ``key_envelope`` holds the passphrase-wrapped DEK — store it LOCALLY only.
    """
    dek = os.urandom(KEY_LEN)
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, plaintext, PAYLOAD_MAGIC)
    payload_env = PAYLOAD_MAGIC + nonce + ct
    return payload_env, _wrap_dek(dek, passphrase)


def _unwrap_dek(key_env: bytes, passphrase: str) -> bytes:
    if key_env[: len(KEY_MAGIC)] != KEY_MAGIC:
        raise CryptoError("bad key envelope magic")
    off = len(KEY_MAGIC)
    time, memory, lanes, salt_len = struct.unpack_from(">IIIB", key_env, off)
    if not (1 <= time <= ARGON2_MAX_TIME
            and 1 <= memory <= ARGON2_MAX_MEMORY
            and 1 <= lanes <= ARGON2_MAX_LANES):
        raise CryptoError(
            "key envelope KDF parameters out of bounds — refusing to derive "
            "(corrupt or tampered key file)"
        )
    off += struct.calcsize(">IIIB")
    salt = key_env[off : off + salt_len]
    off += salt_len
    knonce = key_env[off : off + NONCE_LEN]
    off += NONCE_LEN
    wrapped = key_env[off:]
    kek = _derive_kek(passphrase, salt, time=time, memory=memory, lanes=lanes)
    try:
        return AESGCM(kek).decrypt(knonce, wrapped, KEY_MAGIC)
    except Exception as e:  # InvalidTag → wrong passphrase / tampered key file
        raise CryptoError("could not unwrap key — wrong passphrase or corrupt key file") from e


def decrypt(payload_env: bytes, key_env: bytes, passphrase: str) -> bytes:
    """Inverse of :func:`encrypt`. Needs the payload envelope + the local key envelope."""
    dek = _unwrap_dek(key_env, passphrase)
    if payload_env[: len(PAYLOAD_MAGIC)] != PAYLOAD_MAGIC:
        raise CryptoError("bad payload envelope magic")
    off = len(PAYLOAD_MAGIC)
    nonce = payload_env[off : off + NONCE_LEN]
    ct = payload_env[off + NONCE_LEN :]
    try:
        return AESGCM(dek).decrypt(nonce, ct, PAYLOAD_MAGIC)
    except Exception as e:
        raise CryptoError("payload authentication failed — corrupt or tampered ciphertext") from e


class Keystore:
    """Local store of wrapped DEKs, one file per archived txid (chmod 600).

    This is the deletable half of crypto-shred. The directory holds no plaintext
    and no unwrapped keys — every file is a passphrase-locked key envelope.
    """

    def __init__(self, key_dir: Path):
        self.key_dir = Path(key_dir)
        self.key_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.key_dir.chmod(0o700)
        except OSError:
            pass

    def _path(self, txid: str) -> Path:
        # txids are base64url (a-z A-Z 0-9 - _) — safe as a filename, but guard anyway.
        safe = "".join(c for c in txid if c.isalnum() or c in "-_")
        return self.key_dir / f"{safe}.key"

    def put(self, txid: str, key_env: bytes) -> None:
        p = self._path(txid)
        p.write_bytes(key_env)
        try:
            p.chmod(0o600)
        except OSError:
            pass

    def get(self, txid: str) -> bytes | None:
        p = self._path(txid)
        return p.read_bytes() if p.exists() else None

    def shred(self, txid: str) -> bool:
        """Destroy the key for one archive → its permanent ciphertext can no longer
        be decrypted. Returns True if a key was present and removed.

        The security boundary is the ``unlink`` (no key → AES-GCM payload is
        undecryptable). The in-place overwrite below is best-effort only and is a
        NO-OP on copy-on-write / log-structured / wear-leveling filesystems (e.g.
        WSL2 over NTFS, SSDs, btrfs/ZFS) and does nothing about prior backups or
        filesystem snapshots — do NOT rely on it to scrub the bytes from the medium."""
        p = self._path(txid)
        if not p.exists():
            return False
        # Best-effort overwrite before unlink. NOT a guarantee on modern filesystems
        # (see docstring); the actual shred is the unlink + the key being gone.
        try:
            n = p.stat().st_size
            with open(p, "r+b", buffering=0) as f:
                f.write(os.urandom(n))
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass
        p.unlink()
        return True

    def has(self, txid: str) -> bool:
        return self._path(txid).exists()
