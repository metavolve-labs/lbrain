"""Read-time identity check (CSO design, 2026-11-17T11:15Z).

`identity.json` is a CLAIM: a name and a member public key. This module looks for the EVIDENCE at read time: the
member record for that name and the chain records (Metavolve root -> intermediate -> org -> member) in a records
directory, and verifies them now with the same function the install tool uses. It answers one of three states and
never a bare "chain-verified":

  verified now              the record verifies under the pinned root, its member_public_key equals the claimed
                            address, and today is inside issued..expires; the detail carries the expiry and the
                            time of the check
  claimed, evidence not reachable   the records directory is missing, holds no record for the name, or the verifier
                            (lbrain_teams) is not importable; a FLAG for a seat gate, never a pass
  FAILS                     a record exists and does NOT verify, or names a different key, or is outside its term;
                            a seat gate treats this as a refusal

What it still cannot prove, said in `limits`: a verified record proves the NAME is a member and which public key
belongs to it; it does not prove the process asking holds the matching private key (that needs a signed challenge,
FX-035); and it reads files, so it is only as current as the records directory.

The pinned root is a constant here as it is in the mail adapter (studio/orgid.py pins the org key). A records
directory whose root record does not match it is "FAILS", not a different trust anchor.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

# The Metavolve root public key (ed25519, hex), from the 2026-08-21 root ceremony; signs the org root records.
METAVOLVE_ROOT_PUBLIC_HEX = "aa58159087d78ecc1350fb3f28ebcec18ae4e24a77d8b38f694bbcb1760c5ed5"
# The intermediate key (2026-10-27 hop) and the metavolvelabs org key, pinned so a swapped records directory fails.
INTERMEDIATE_PUBLIC_HEX = "29ec34f30356dc626883474b1b0a92e63298d2d5d0520aa451fb0c0080c3f798"
ORG_PUBLIC_HEX = {"metavolvelabs": "35ffc75f7964b7c8ceebb32be4a2193c744ff5d2ce8b2d171109612cd384de59"}

STATE_VERIFIED = "verified now"
STATE_UNREACHABLE = "claimed, evidence not reachable"
STATE_FAILS = "FAILS"

LIMITS = ("a verified record proves the name is a member and which public key is its; it does not prove this process "
          "holds the private key (a signed challenge does that, per-seat-user work); the check reads files and is only "
          "as current as the records directory")


def default_records_dir() -> Path | None:
    """The estate records of the lbrain-teams checkout the engine can see (a sibling of this repo), else None."""
    here = Path(__file__).resolve()
    for up in here.parents:
        cand = up / "lbrain-teams" / "records"
        if (cand / "intermediate-record.json").is_file():
            return cand
    return None


def _import_teams(records: Path):
    """lbrain_teams lives beside the records (records/..); add it to sys.path if it is not installed."""
    try:
        import lbrain_teams  # noqa: F401
    except ModuleNotFoundError:
        sys.path.insert(0, str(records.parent))
    from lbrain_teams.membership import Membership, MembershipError, verify_chained
    from lbrain_teams.intermediate import IntermediateChain, IntermediateRecord, org_signing_keys
    from lbrain_teams.root import RootChain, RootRecord
    return Membership, MembershipError, verify_chained, IntermediateChain, IntermediateRecord, org_signing_keys, RootChain, RootRecord


def pins_from_config(cfg) -> dict:
    """The trust anchor a home pins: its own (config, a founder's drive) or the Metavolve defaults (empty config).
    Returns {"root": hex, "intermediate": hex, "orgs": {org: hex}}; a malformed org entry falls back to the default."""
    root = (getattr(cfg, "estate_root_public_hex", "") or "").strip().lower() or METAVOLVE_ROOT_PUBLIC_HEX
    inter = (getattr(cfg, "estate_intermediate_public_hex", "") or "").strip().lower() or INTERMEDIATE_PUBLIC_HEX
    orgs = dict(ORG_PUBLIC_HEX)
    raw = (getattr(cfg, "estate_org_public_hex", "") or "").strip()
    if "=" in raw:
        o, h = raw.split("=", 1)
        if o.strip() and h.strip():
            orgs = {o.strip(): h.strip().lower()}
    return {"root": root, "intermediate": inter, "orgs": orgs}


def check(name: str, address: str, records_dir: str | Path | None = None, today: dt.date | None = None,
          pins: dict | None = None) -> dict:
    """Return {"state", "detail", "checked_at", "expires", "records_dir", "limits"}; never raises.
    `pins` (2026-11-18): the trust anchor to verify under; None = the Metavolve defaults. A founder's home passes
    pins_from_config(cfg) so its own root is the anchor and ours is NOT above it."""
    pins = pins or {"root": METAVOLVE_ROOT_PUBLIC_HEX, "intermediate": INTERMEDIATE_PUBLIC_HEX, "orgs": dict(ORG_PUBLIC_HEX)}
    checked_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"state": STATE_UNREACHABLE, "detail": "", "checked_at": checked_at, "expires": None,
           "records_dir": None, "limits": LIMITS}
    name = (name or "").strip()
    if name.startswith("gcx://"):
        name = name[len("gcx://"):]
    if not name:
        out["detail"] = "no name claimed"; return out
    records = Path(records_dir).expanduser() if records_dir else default_records_dir()
    if records is None or not records.is_dir():
        out["detail"] = f"records directory not reachable ({records or 'none configured, none found beside the engine'})"
        return out
    out["records_dir"] = str(records)
    org = name.split("/", 1)[0]
    # The file slug is not derivable from the name (founding records drop the csuite/agents segment, the 2026-11-17
    # mints keep it), so the estate is scanned for the record whose `member` field IS the name.
    rec_path = None
    for cand in sorted((records / "estate").glob("member-*.json")):
        try:
            if json.loads(cand.read_text(encoding="utf-8")).get("member") == name:
                rec_path = cand; break
        except (OSError, json.JSONDecodeError):
            continue
    if rec_path is None:
        out["detail"] = f"no member record for {name} in {records / 'estate'}"; return out
    try:
        (Membership, MembershipError, verify_chained, IntermediateChain, IntermediateRecord,
         org_signing_keys, RootChain, RootRecord) = _import_teams(records)
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"verifier not importable ({type(e).__name__})"; return out
    try:
        inter = json.loads((records / "intermediate-record.json").read_text(encoding="utf-8"))
        org_rec = json.loads((records / "estate" / f"org-root-{org}.json").read_text(encoding="utf-8"))
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out["detail"] = f"chain records not readable ({type(e).__name__})"; return out
    # pins: a records directory with other keys is a failure, not another anchor
    if inter.get("public_key") != pins["intermediate"]:
        out["state"] = STATE_FAILS; out["detail"] = "intermediate record is not the pinned intermediate key"; return out
    if pins["orgs"].get(org) and org_rec.get("public_key") != pins["orgs"][org]:
        out["state"] = STATE_FAILS; out["detail"] = f"org root record for {org} is not the pinned org key"; return out
    try:
        root_pub = bytes.fromhex(pins["root"])
        inter_chain = IntermediateChain([IntermediateRecord(**inter)])
        org_chain = RootChain(org, [RootRecord(**org_rec)])
        signers = org_signing_keys(root_pub, inter_chain)
        org_chain.verify(signers)
        m = Membership(**{k: rec[k] for k in ("member", "member_public_key", "org", "issued", "expires", "signature")})
        verify_chained(m, org_chain, signers, today=today)
    except Exception as e:  # noqa: BLE001  (MembershipError, RootError, KeyError, ValueError: all "does not verify")
        out["state"] = STATE_FAILS; out["detail"] = f"record does not verify under the pinned root: {type(e).__name__}: {str(e)[:120]}"
        return out
    if m.member != name:
        out["state"] = STATE_FAILS; out["detail"] = f"record names {m.member}, not {name}"; return out
    if not (address or "").strip():
        # CSO 2026-11-17T11:34Z: an EMPTY claim must not read as verified; the claim is the pair (name, key)
        out["state"] = STATE_FAILS; out["detail"] = "no address claimed; the record binds a key and the claim names none"; return out
    if m.member_public_key != address.strip():
        out["state"] = STATE_FAILS; out["detail"] = "record's member_public_key is not the claimed address"; return out
    today = today or dt.date.today()
    try:
        issued = dt.date.fromisoformat(m.issued); expires = dt.date.fromisoformat(m.expires)
    except ValueError:
        out["state"] = STATE_FAILS; out["detail"] = "record dates unreadable"; return out
    if not (issued <= today <= expires):
        out["state"] = STATE_FAILS; out["detail"] = f"outside term {m.issued}..{m.expires}"; out["expires"] = m.expires; return out
    out["state"] = STATE_VERIFIED
    out["expires"] = m.expires
    out["detail"] = f"member of {org} under the pinned root; record {rec_path.name}; expires {m.expires}; checked {checked_at}"
    return out
