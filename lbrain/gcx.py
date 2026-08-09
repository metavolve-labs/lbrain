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

import base64
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass

GRAPHQL = "https://arweave.net/graphql"
GATEWAY = "https://arweave.net"

# The gcx:// operator wallet (A-506, operator-signed pointer records). This value is anchored in
# the gcx:// URI-scheme specification itself — the one artifact a verifier must already read to
# know what gcx:// means — and duplicated here so resolution can check it. Changing operators is
# a specification revision plus an engine release, never a server-side swap: "trust the key our
# server hands you" is exactly the dependency this scheme exists to remove.
OPERATOR_ADDRESS = "BPLL7nZOmxMIveXkbt59Yotve0IDM-UCCunPFe2imUc"

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
    # A-506: txid of the operator-signed pointer record that selected this transaction,
    # or None when resolution was by uniqueness (the legacy path).
    authority_txid: str | None = None

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


def _tagmap(n):
    return {t["name"]: t["value"] for t in n.get("tags", [])}


def _address_from_owner_key(owner_key: str) -> str | None:
    """Derive an Arweave address from the owner PUBLIC KEY, locally.

    address = Base64URL( SHA-256( raw public-key bytes ) ), and `owner.key` is the
    base64url RSA modulus. The address is therefore a FUNCTION of the key — not a
    fact a gateway gets to assert.

    G1. `_authority_target` filtered on `owners:` and then re-read `owner.address`,
    but both come from the gateway. A hostile or compromised gateway could hand back
    any transaction and label it ours. Deriving the address from the key it also
    returns removes that: to fool this, a gateway must produce a public key that
    SHA-256s to the operator's address, which it cannot.

    hashlib only — no crypto dependency, no pending decision.
    """
    try:
        pad = "=" * (-len(owner_key) % 4)
        raw = base64.urlsafe_b64decode(owner_key + pad)
    except Exception:
        return None
    if not raw:
        return None
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _authority_target(name: str, *, graphql: str, timeout: float) -> tuple[str, str] | None:
    """(authority record id, canonical target txid) per the operator-signed pointer
    records for `name`, or None when no valid record exists.

    A-506: anyone can mint a `GCX-Name` tag for any name, so uniqueness is deniable for one
    transaction fee. An AUTHORITY record selects on a verifiable signer instead: only records
    whose on-chain owner is the operator address pinned above (and in the spec) count.

    Supersession is defined BEFORE ship, not after: among the operator's authority records,
    latest-by-block-height wins. Unconfirmed records (no block yet) lose to any confirmed one;
    more than one unconfirmed record with no confirmed anchor cannot be ordered and refuses.

    On gateway failure this returns None — resolution then degrades to the legacy
    refuse-on-ambiguity behaviour, which never resolves LESS safely than the pre-authority
    engine did (a suppressed authority query can re-create yesterday's refusal, never a
    wrong answer).
    """
    query = (
        "query($n:[String!],$o:[String!]){transactions(tags:[{name:\"GCX-Authority\","
        "values:$n}],owners:$o,first:10){edges{node{id tags{name value} "
        "owner{address key} block{height}}}}}"
    )
    try:
        data = _post_json(
            graphql, {"query": query, "variables": {"n": [name], "o": [OPERATOR_ADDRESS]}}, timeout
        )
    except Exception:
        return None
    edges = (data.get("data") or {}).get("transactions", {}).get("edges") or []
    records = []
    for e in edges:
        n = e.get("node") or {}
        # G1: derive the address from the returned public KEY rather than believing
        # the gateway's `address` field. A record whose key is absent, undecodable,
        # or hashes to anything else is not ours — drop it. Dropping every record
        # degrades to legacy refuse-on-ambiguity, which is the safe direction.
        owner = n.get("owner") or {}
        derived = _address_from_owner_key(owner.get("key") or "")
        if derived is None or derived != OPERATOR_ADDRESS:
            continue
        target = _tagmap(n).get("GCX-Target")
        if not target:
            continue
        height = (n.get("block") or {}).get("height")
        records.append((height, n.get("id"), target))
    if not records:
        return None
    confirmed = [r for r in records if r[0] is not None]
    if confirmed:
        chosen = max(confirmed, key=lambda r: r[0])
    elif len(records) == 1:
        chosen = records[0]
    else:
        raise ResolveError(
            f"{name} has {len(records)} unconfirmed authority records — they cannot be "
            "ordered until at least one is mined; refusing to guess"
        )
    return chosen[1], chosen[2]  # (authority record id, canonical target txid)


def _lookup_by_id(txid: str, *, graphql: str, timeout: float) -> tuple[str, dict]:
    query = (
        "query($ids:[ID!]){transactions(ids:$ids,first:1){edges{node{id tags{name value}}}}}"
    )
    try:
        data = _post_json(graphql, {"query": query, "variables": {"ids": [txid]}}, timeout)
    except Exception as e:
        raise ResolveError(f"gateway query failed ({graphql}): {e}") from e
    edges = (data.get("data") or {}).get("transactions", {}).get("edges") or []
    if not edges:
        raise ResolveError(
            f"authority record points at {txid}, which this gateway cannot find — "
            "refusing to resolve past a dangling pointer"
        )
    node = edges[0]["node"]
    return node["id"], _tagmap(node)


def _lookup_full(
    name: str, *, graphql: str = GRAPHQL, timeout: float = 30.0
) -> tuple[str, dict, str | None]:
    """(txid, tags, authority_txid_or_None) — see lookup() for the contract."""
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

    # One gcx:// name legitimately covers two transactions: the payload and a
    # JSON metadata sidecar. Select on SEMANTICS, not on a naming convention —
    # a first attempt filtered `Type` ending in "sidecar" and missed, because the
    # real sidecar is typed `GCX-PAPR-H` (verified 2026-07-29). Two self-
    # describing signals distinguish them, and neither depends on a string guess:
    #   * the sidecar points AT the payload via `Fulltext-Tx`
    #   * only the payload carries `Canonical-SHA256`
    tagged = [(n, _tagmap(n)) for n in nodes]
    payloads = [(n, t) for n, t in tagged if "Fulltext-Tx" not in t]
    hashed = [(n, t) for n, t in payloads if t.get("Canonical-SHA256")]
    chosen = hashed or payloads or tagged

    # A-506: an operator-signed pointer record, when one exists, selects the canonical
    # transaction by VERIFIABLE SIGNER rather than by uniqueness. Additive and opt-in:
    # with no authority record, behaviour below is byte-for-byte the legacy engine —
    # including the refusal — so the 9,806 already-minted records need no backfill, and
    # the record doubles as a REMEDY: a squatted name recovers the day an authority
    # record is mined for it.
    authority = _authority_target(name, graphql=graphql, timeout=timeout)
    if authority:
        auth_id, target = authority
        pointed = [(n, t) for n, t in chosen if n["id"] == target]
        if pointed:
            node, tags = pointed[0]
            return node["id"], tags, auth_id
        node_id, tags = _lookup_by_id(target, graphql=graphql, timeout=timeout)
        return node_id, tags, auth_id

    if len(chosen) > 1:
        ids = ", ".join(n["id"] for n, _ in chosen[:5])
        raise ResolveError(
            f"{name} is claimed by {len(chosen)} transactions ({ids}) — refusing to "
            "guess which is canonical"
        )
    node, tags = chosen[0]
    return node["id"], tags, None


def lookup(name: str, *, graphql: str = GRAPHQL, timeout: float = 30.0) -> tuple[str, dict]:
    """(txid, tags) for a gcx:// name, by on-chain tag query.

    Raises if the name is unknown, or if more than one transaction claims it and no
    operator-signed authority record disambiguates — an ambiguous name must never
    resolve silently to whichever came back first.
    """
    txid, tags, _authority = _lookup_full(name, graphql=graphql, timeout=timeout)
    return txid, tags


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
    txid, tags, authority = _lookup_full(name, graphql=graphql, timeout=min(timeout, 30.0))
    content = fetch(txid, gateway=gateway, timeout=timeout)
    expected = (tags.get("Canonical-SHA256") or "").strip().lower()
    actual = hashlib.sha256(content).hexdigest()
    return Resolved(
        name=name, txid=txid, expected_sha256=expected, actual_sha256=actual,
        raw_content=content, tags=tags, gateway=gateway, authority_txid=authority,
    )
