# Design — `gcx://` authority verification (A-506 / G1)

**Status:** DESIGN ONLY, no code. CSO, 2026-08-09, per the CTO's routing (*"Design PR first
per your rule; I did NOT touch gcx.py"*).
**Closes:** A-506 · spec §7.3 · G1 of the PPA5 enablement audit.

---

## 0. A flag before the analysis

The CTO's G1 names `_authority_target` as verifying *"the gateway-reported `owner.address`
STRING, not a cryptographic signature."* **That function does not exist anywhere in
`metavolve-labs/lbrain`** — not on `main`, not on any branch, and not in any commit's
history (`git log --all -S`). It is presumably in unpushed work or the pipeline repo, both
of which are outside my reach.

**I have not reviewed that function and this design does not assume it.** What follows is
written against `lbrain/gcx.py` as it exists on `main` today, which I can read. If
`_authority_target` differs from what I describe, the gap below still stands — because
today there is no authority check of any kind to differ from.

## 1. The gap, stated precisely

`grep` for `owner|signature|sign|authority|pubkey` across `lbrain/gcx.py` returns **three
hits, all prose in comments.** There is no authenticity check in the resolver.

What resolution actually does today:

1. `lookup()` asks a gateway's GraphQL for transactions tagged `GCX-Name: <the name>`.
2. It selects among them by **tag shape** — prefers a node carrying `Canonical-SHA256` and
   lacking `Fulltext-Tx`. Refuses if more than one survives.
3. `resolve()` fetches the payload and compares its SHA-256 against the `Canonical-SHA256`
   **tag on that same transaction**.

**The check is circular.** The bytes and the hash they are checked against are both supplied
by whoever minted the transaction. Anyone can mint a transaction with any `GCX-Name` tag.
So the hash proves the payload was not corrupted **in transit**; it proves nothing about
**who said it**.

Two consequences, and the second is worse:

- **Ambiguity → denial.** Mint a competing claim on an existing name and resolution refuses
  (§7.3, already documented). Cheap DoS, permanent, no delete.
- **🔴 Unminted name → silent authority.** For a name we have *not* minted, an attacker's
  transaction is the **only** candidate. It passes shape selection, its hash matches its own
  payload, and `resolve()` returns **`VERIFIED`**. A third party using our own reference
  implementation is told, in our vocabulary, that attacker content is authentic.

**This directly falsifies the spec's §3.1 utility claim** — the one I wrote, and the one the
permanent registration will rest on:

> *"the expected hash is fetched from an on-chain tag … not from this library, and not from
> any server the registrant operates. A third party … can independently determine whether a
> record is authentic."*

The hash *does* come from the chain. But **from whoever wrote the transaction.** "On-chain"
was doing work in that sentence that it cannot bear. The property we are registering is not
implemented, and the spec must not be submitted claiming it until it is.

## 2. What "verified" must mean

**Today:** *these bytes match a hash someone published alongside them.*
**Required:** *these bytes match a hash published by the party we recognise as authoritative
for this name.*

That requires binding a name to a **key**, not to a transaction — and checking a signature,
not a reported field.

## 3. Design

**Trust anchor.** The operator public key is **pinned in the specification** (spec §D7),
which is the one artifact a verifier must already read to know what `gcx://` means. It must
**not** be fetched from a gateway or from any Metavolve endpoint — that reintroduces exactly
the dependency the scheme exists to remove. (This is the adopted note from the earlier
routing; restating because it is the load-bearing constraint.)

**Authority record.** An Arweave transaction, signed by the operator key, asserting:
*for name N, the canonical payload is transaction T.* Tags: `GCX-Authority: <name>`,
`GCX-Canonical-Tx: <txid>`.

**Resolution becomes:**

1. Query for `GCX-Authority` records naming N.
2. **Verify each candidate's signature against the pinned key locally** — from the
   transaction's own signature and owner public key, *not* from a gateway-reported
   `owner.address` string. A gateway that lies about `owner.address` is exactly the threat
   model; a gateway cannot forge a signature.
3. Among signature-valid authority records, take the **highest block height**
   (supersession = latest-by-block-height, declared before ship). **Ties refuse** — G3.
4. Fetch the transaction it names; verify `Canonical-SHA256` as today.
5. **No valid authority record → status is `UNVERIFIED-AUTHORITY`, never `VERIFIED`.**

**Additive and opt-in.** Absent an authority record, today's shape-selection plus
refuse-on-ambiguity stands unchanged. No backfill, no AR spend against 9,806 existing
records, no breaking change — **and it makes the mechanism a remedy**: a squatted name is
recoverable by minting one authority record, where today it is permanently dead.

## 4. Status vocabulary — the honest part

`gcx.Resolved.status` currently returns three values and its comment reads *"Absence is
reported as absence — never as a pass."* Authority must obey the same rule:

| status | meaning |
|---|---|
| `VERIFIED` | signature-valid authority record **and** payload hash matches |
| `HASH MISMATCH` | authority valid, bytes wrong — refuse |
| `UNVERIFIED-AUTHORITY` | **no signature-valid authority record** — hash may match, authenticity unknown |
| `UNVERIFIABLE` | no `Canonical-SHA256` recorded (existing) |
| `AMBIGUOUS` | tie at highest block height, or competing claims with no authority — refuse (G3) |

**`UNVERIFIED-AUTHORITY` must not be collapsed into `VERIFIED`.** That collapse is the whole
bug, and it is the same shape as `whoami` reporting self-asserted credentials with no marker
(issue #17) — a surface that cannot distinguish attested from asserted.

## 5. G2 — enforcement belongs in the resolver

`resolve()` returns payload bytes in **every** state; refusal lives only at the MCP layer.
Any other caller — the CLI, a library user, a future surface — gets bytes with a status they
may ignore. **Gate byte-return on the status inside `resolve()`**, so "refuse rather than
degrade" is a property of the resolver rather than a habit of one caller. This is the
nine-surfaces argument from #18: a rule enforced at one call site is enforced nowhere.

## 6. How this gets verified

Per CONTRIBUTING rule 7 — outcome, not mechanism:

1. **Forged authority record** signed by a non-operator key → must yield `UNVERIFIED-AUTHORITY`,
   never `VERIFIED`.
2. **Lying gateway.** Mock a gateway reporting an `owner.address` equal to the operator's while
   the transaction is signed by another key → must fail. *This is the exact G1 case and the
   test that would have caught it.*
3. **Unminted name, attacker-only claim** → `UNVERIFIED-AUTHORITY`, not `VERIFIED`.
4. **Supersession** — two valid authority records, higher block height wins; **equal heights
   refuse** (G3).
5. **No authority record** → behaviour identical to today, byte-for-byte.
6. **Mutation test the check**: disable signature verification and assert tests 1–3 fail. A
   test suite that passes with the check disabled is testing nothing.

## 7. Open questions for the CTO

1. **Key rotation.** A pinned key in a published spec is hard to rotate. Chain of authority
   records signed by the old key naming the new one? That is a design in itself and it should
   exist before the key is pinned, not after.
2. **Which library verifies the signature?** Arweave signatures are RSA-PSS over a specific
   payload encoding. `lbrain` currently has no crypto dependency, and adding one to a package
   whose selling point is a small dependency-free footprint is a real cost. Vendored minimal
   verifier, or an optional extra?
3. **Does `aet://` share the operator key or hold its own?** Trajectory says the Aeternum
   Foundation eventually operates `aet://`; a shared key would need splitting at that point.
4. **Do we mint authority records for the 9,806 existing RFCs?** My design says no, opt-in.
   That means those names stay `UNVERIFIED-AUTHORITY` — which is *honest*, but it is also
   most of the corpus. Worth an explicit decision rather than a default.
