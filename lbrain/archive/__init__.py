"""LBrain Tier-2 — optional permanent, verifiable, encrypted episodic archive.

This is a self-contained subpackage with a strict one-way dependency on the core
(``archive`` may import core; core never imports ``archive`` except through guarded,
lazy registration). Installing without the ``archive`` extra omits it entirely; the
core retrieval engine (index → embed → store → search → MCP) runs unchanged.

Registration helpers:
    lbrain.archive.cli.register(main_group)   — adds the archive CLI commands
    lbrain.archive.mcp.register(mcp_server)    — adds the lair_deep_recall MCP tool
"""

from __future__ import annotations

from .archiver import (
    ArchiveResult,
    Archiver,
    ArweaveL1Transport,
    LocalTransport,
    _content_txid,
    _fetch_gcp_secret,
    _load_arweave_wallet,
    make_snapshot,
    make_transport,
    verify_on_chain,
)
from .config import archive_passphrase, set_archive_passphrase
from .crypto import CryptoError, Keystore
from .storage import ArchiveStore

__all__ = [
    "Archiver",
    "ArchiveResult",
    "LocalTransport",
    "ArweaveL1Transport",
    "ArchiveStore",
    "Keystore",
    "CryptoError",
    "make_transport",
    "make_snapshot",
    "verify_on_chain",
    "archive_passphrase",
    "set_archive_passphrase",
    "_content_txid",
    "_fetch_gcp_secret",
    "_load_arweave_wallet",
]
