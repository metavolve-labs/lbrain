"""`gcx://` resolution — name → permanent record, verified against the chain.

`gcx` and `aet` are IANA-registered provisional URI schemes (2026-07-28). This
module resolves such a name to its Arweave transaction and checks the bytes it
fetched against a hash that was written **on-chain at mint time**.

The important property is where the hash comes from. It is an Arweave tag
(``Canonical-SHA256``) on the transaction itself — not a value this library
ships, and not a value our server hands you. So a stranger with this code and a
public gateway can verify a record without trusting Metavolve Labs at any point.
That is the whole claim reduced to one command:

    lbrain resolve gcx://rfc/793

Resolution is registry-free by design. An earlier draft planned to ship (or
host) ``rfc_full_registry.json`` — 4.9 MB, and it would have made us the
authority on our own provenance. Querying the chain by tag removes both
problems.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass

GRAPHQL = "https://arweave.net/graphql"
GATEWAY = "https://arweave.net"

# scheme://collection/id  — e.g. gcx://rfc/793, aet://works/0020
_NAME = re.compile(r"^(?P<scheme>gcx|aet)://(?P<path>[A-Za-z0-9._~/-]+)$")

SCHEMES = ("gcx", "aet")


class ResolveError(RuntimeError):
    pass


@dataclass
class Resolved:
    name: str
    txid: str
    expected_sha256: str          # from the ON-CHAIN tag, never from us
    actual_sha256: str
    raw_content: bytes            # NEVER hand this to a caller — see `content`
    tags: dict
    gateway: str

    @property
    def content(self) -> bytes:
        """The payload — ONLY when it verified. Raises otherwise.

        G2 (2026-08-09). `resolve()` used to return bytes in every state and left
        refusal to whoever happened to be calling. Exactly one caller enforced it
        (the MCP resource); the CLI wrote `--out` and printed `--quiet` BEFORE
        checking, so `lbrain resolve … --quiet > file` captured unverified bytes
        and reported the failure afterwards — by which point the shell had the
        content. The exit code was correct and useless.

        "Refuse rather than degrade" has to be a property of the resolver, not a
        habit of its callers: a rule enforced at one call site is enforced
        nowhere. Deliberate handling of unverified bytes uses `raw_content`,
        which is greppable precisely because it should be rare.
        """
        if not self.verified:
            raise ResolveError(
                f"refusing to return unverified content for {self.name}: {self.status}. "
                f"Use .raw_content only if you intend to handle unverified bytes."
            )
        return self.raw_content

    @property
    def verified(self) -> bool:
        """True only if the chain recorded a hash AND the bytes match it."""
        return bool(self.expected_sha256) and self.expected_sha256 == self.actual_sha256

    @property
    def status(self) -> str:
        if not self.expected_sha256:
            # Not every minted record carries the tag (verified 2026-07-29:
            # RFC 793 has Canonical-SHA256, RFC 2616 does not). Absence is
            # reported as absence — never as a pass.
            return "UNVERIFIABLE (no hash recorded on-chain)"
        return "VERIFIED" if self.verified else "HASH MISMATCH"


def parse(name: str) -> tuple[str, str]:
    """('gcx', 'rfc/793') — or raise. Case-normalizes the scheme only."""
    m = _NAME.match(name.strip())
    if not m:
        raise ResolveError(
            f"not a resolvable name: {name!r} — expected gcx://<collection>/<id> "
            "(e.g. gcx://rfc/793)"
        )
    return m.group("scheme").lower(), m.group("path")


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def lookup(name: str, *, graphql: str = GRAPHQL, timeout: float = 30.0) -> tuple[str, dict]:
    """(txid, tags) for a gcx:// name, by on-chain tag query.

    Raises if the name is unknown, or if more than one transaction claims it —
    an ambiguous name must never resolve silently to whichever came back first.
    """
    parse(name)  # validate before spending a round trip
    query = (
        "query($n:[String!]){transactions(tags:[{name:\"GCX-Name\",values:$n}],"
        "first:10){edges{node{id tags{name value}}}}}"
    )
    try:
        data = _post_json(graphql, {"query": query, "variables": {"n": [name]}}, timeout)
    except Exception as e:
        raise ResolveError(f"gateway query failed ({graphql}): {e}") from e

    edges = (data.get("data") or {}).get("transactions", {}).get("edges") or []
    # A record may be minted as fulltext + sidecar under one name; prefer the
    # payload, not the metadata.
    nodes = [e["node"] for e in edges]
    if not nodes:
        raise ResolveError(f"{name} is not registered on this gateway")

    def tagmap(n):
        return {t["name"]: t["value"] for t in n.get("tags", [])}

    # One gcx:// name legitimately covers two transactions: the payload and a
    # JSON metadata sidecar. Select on SEMANTICS, not on a naming convention —
    # a first attempt filtered `Type` ending in "sidecar" and missed, because the
    # real sidecar is typed `GCX-PAPR-H` (verified 2026-07-29). Two self-
    # describing signals distinguish them, and neither depends on a string guess:
    #   * the sidecar points AT the payload via `Fulltext-Tx`
    #   * only the payload carries `Canonical-SHA256`
    tagged = [(n, tagmap(n)) for n in nodes]
    payloads = [(n, t) for n, t in tagged if "Fulltext-Tx" not in t]
    hashed = [(n, t) for n, t in payloads if t.get("Canonical-SHA256")]
    chosen = hashed or payloads or tagged

    if len(chosen) > 1:
        ids = ", ".join(n["id"] for n, _ in chosen[:5])
        raise ResolveError(
            f"{name} is claimed by {len(chosen)} transactions ({ids}) — refusing to "
            "guess which is canonical"
        )
    node, tags = chosen[0]
    return node["id"], tags


def fetch(txid: str, *, gateway: str = GATEWAY, timeout: float = 60.0) -> bytes:
    """Raw bytes for a txid.

    urllib follows the gateway's 302 automatically. Note for anyone porting this
    to curl: `-L` is REQUIRED — arweave.net redirects, and without it you hash
    an error page and get a mismatch you will spend an hour misreading.
    """
    url = f"{gateway.rstrip('/')}/{txid}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        raise ResolveError(f"fetch failed ({url}): {e}") from e


def resolve(
    name: str,
    *,
    gateway: str = GATEWAY,
    graphql: str = GRAPHQL,
    timeout: float = 60.0,
) -> Resolved:
    """Resolve a gcx://name, fetch it, and verify it against the chain."""
    txid, tags = lookup(name, graphql=graphql, timeout=min(timeout, 30.0))
    content = fetch(txid, gateway=gateway, timeout=timeout)
    expected = (tags.get("Canonical-SHA256") or "").strip().lower()
    actual = hashlib.sha256(content).hexdigest()
    return Resolved(
        name=name, txid=txid, expected_sha256=expected, actual_sha256=actual,
        raw_content=content, tags=tags, gateway=gateway,
    )
