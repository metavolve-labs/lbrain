"""Archive-side config helpers — passphrase resolution for the Tier-2 archive.

Kept in the archive subpackage (not core ``config.py``) so the core config layer
has no archive-specific behavior. The arweave_* *fields* still live on the core
``Config`` dataclass (passive data; persisted by ``Config.write``), but the logic
that resolves the encryption passphrase belongs here.
"""

from __future__ import annotations

import os


def archive_passphrase() -> str:
    """The Tier-2 archive passphrase. Sourced from ~/.lbrain/env (chmod 600), NEVER
    config.toml. The env value may be the literal passphrase OR a runtime reference
    ``gcp-secret:<project>/<secret>`` (resolved from GCP Secret Manager, like the wallet)
    so the actual secret lives only in IAM-controlled storage and the local file holds a
    pointer. Empty if unset; callers prompt interactively."""
    val = os.environ.get("LBRAIN_ARCHIVE_PASSPHRASE", "").strip()
    if val.startswith(("gcp-secret:", "gcp:")):
        from .archiver import _fetch_gcp_secret

        body = val.split(":", 1)[1]
        if "/" not in body:
            return ""
        project, secret = body.split("/", 1)
        return _fetch_gcp_secret(project, secret).strip()
    return val


def set_archive_passphrase(passphrase: str) -> None:
    """Persist the archive passphrase to the 600 env file (same secret pattern as keys)."""
    from ..config import _write_env_var  # core secret-file writer (atomic 0600)

    _write_env_var("LBRAIN_ARCHIVE_PASSPHRASE", passphrase)
    os.environ["LBRAIN_ARCHIVE_PASSPHRASE"] = passphrase
