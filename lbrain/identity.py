"""Identity — who this brain is, and what it is trusted for.

A brain that serves records into an agent's context should be able to say what
it is. Today most of that is local facts (what is indexed, how it serves). The
ecosystem half — a `gcx://` name, a key, and the credentials that name has
earned — arrives with the registry; this module is where it lands, and until
then it reports *unregistered* rather than inventing a status.

Design note, deliberate: an unregistered brain is a first-class state, not an
error. Identity accretes onto an anonymous install; it never gates one.

The record lives at ``~/.lbrain/identity.json`` (0600, same posture as the
secret store — it holds a key).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CONFIG_DIR

IDENTITY_PATH = CONFIG_DIR / "identity.json"


@dataclass
class Identity:
    """A registered ecosystem identity. Absent file = unregistered."""

    name: str = ""                          # the gcx:// label, without scheme
    address: str = ""                       # wallet / key address that owns it
    credentials: list[str] = field(default_factory=list)   # verified cred types
    trust_score: float | None = None        # last known bureau score, if any
    registered_at: str = ""                 # ISO date
    issuer: str = ""                        # who attested (never ourselves, for authority)
    # How much of the above was CHECKED. `register` writes plain CLI strings, so the
    # default is the honest one. Only a code path that actually verifies may raise it.
    verification: str = "self-asserted"     # self-asserted | chain-verified

    @property
    def gcx(self) -> str:
        return f"gcx://{self.name}" if self.name else ""

    @classmethod
    def load(cls) -> "Identity | None":
        """The registered identity, or None. Never raises on a damaged file —
        a corrupt identity record must not break retrieval."""
        if not IDENTITY_PATH.exists():
            return None
        try:
            raw = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(
                f"[lbrain] WARNING: {IDENTITY_PATH} is unreadable ({e}); "
                "treating this brain as unregistered.",
                file=sys.stderr,
            )
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass
        body = json.dumps(asdict(self), indent=2).encode("utf-8")
        tmp = IDENTITY_PATH.with_name(IDENTITY_PATH.name + ".tmp")
        # Private from the moment it exists — this record holds a key reference.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, IDENTITY_PATH)


def _serving_db(cfg) -> str:
    from .epoch import serving_db_path
    return serving_db_path(cfg)


def _identity_note(ident) -> str:
    """The one line a consuming agent must read before trusting anything above."""
    if ident is None:
        return ("unregistered — local brain, fully functional; no ecosystem "
                "identity claimed")
    if (ident.verification or "self-asserted") != "chain-verified":
        claimed = []
        if ident.credentials:
            claimed.append("credentials")
        if ident.trust_score is not None:
            claimed.append("a trust score")
        if ident.issuer:
            claimed.append(f"an issuer ({ident.issuer})")
        extra = (" It claims " + ", ".join(claimed) + ", none of which were checked."
                 if claimed else "")
        return ("SELF-ASSERTED — this brain declared this identity locally; nothing "
                "verified it against the chain." + extra)
    return ""


def describe(cfg, stats: dict | None = None) -> dict:
    """The structured answer to 'who am I and what am I trusted for?'.

    Returns plain data so the CLI and the MCP tool render the SAME facts — the
    surfaces must not be able to disagree about identity.
    """
    ident = Identity.load()
    return {
        "identity": {
            "registered": ident is not None,
            "gcx": ident.gcx if ident else "",
            "address": ident.address if ident else "",
            "credentials": ident.credentials if ident else [],
            "trust_score": ident.trust_score if ident else None,
            # issue #17. `whoami` exists to answer whether a brain "carries any
            # credential beyond its own say-so" — and `register` takes credentials,
            # trust_score and issuer as unvalidated CLI strings. Reporting them with
            # no marker made this surface a trust-laundering primitive: any brain
            # could claim any credential and `lair_whoami` would relay it to another
            # agent as fact. Same rule as gcx.Resolved.status — absence of a check is
            # reported as absence, never as a pass.
            "verification": (ident.verification or "self-asserted") if ident else "",
            "issuer": (ident.issuer or "") if ident else "",
            # An unregistered brain is fully functional. Say so, so an agent
            # reading this does not treat absence as breakage.
            "note": _identity_note(ident),
        },
        "brain": {
            "db": _serving_db(cfg),
            "sources": [str(s) for s in getattr(cfg, "sources", [])],
            "docs": (stats or {}).get("docs"),
            "chunks": (stats or {}).get("chunks"),
            "embedded": (stats or {}).get("embedded"),
        },
        "serving_contract": {
            # What a consumer may rely on when reading this brain's output.
            "mode": getattr(cfg, "serve_mode", "structured"),
            "provider": getattr(cfg, "embedding_provider", "?"),
            "attribution": "every served record carries source, chunk and an honest date label",
            "staleness_marked": bool(getattr(cfg, "serve_staleness", True)),
            "untrusted_data_fenced": True,
        },
    }
