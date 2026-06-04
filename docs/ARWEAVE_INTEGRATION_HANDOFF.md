# LBrain × Arweave — Tier-2 Verifiable-Memory Integration (Implementation Handoff)

> **STATUS: MVP SHIPPED — 2026-06-03** (branch `feat/frankenstein`). The full Definition of
> Done loop is verified end-to-end on the offline `local` transport (encrypt → archive →
> index snapshot → semantic recall → fetch-by-txid → **byte-identical** decrypt →
> crypto-shred ⇒ undecryptable). Real Arweave L1 writes are wired (opt-in) but require a
> funded wallet, so they're exercised in real use, not in the test suite.
>
> **What landed:**
> - `lbrain/crypto.py` — AES-256-GCM payloads + Argon2id-derived KEK; **DEK/KEK envelope**
>   so the encryption key is wrapped by the passphrase and stored LOCALLY only
>   (`~/.lbrain/keys/<txid>.key`, 600). Per-item crypto-shred = delete that one file.
> - `lbrain/archive.py` — pluggable transports (`LocalTransport` content-addressed for the
>   offline MVP; `ArweaveL1Transport` via `arweave-python-client`, opt-in), the `Archiver`
>   (encrypt→put→snapshot→index / fetch→decrypt / shred), and structured-bullet snapshot gen
>   (LLM when a key is present, deterministic extractive fallback offline).
> - `store.py` — `archives` table + `vec_archives`/`fts_archives` layer (siloed from the
>   main retrieval path) + methods; idempotent migration on existing DBs.
> - `config.py` — `arweave_enabled / arweave_transport / arweave_wallet_path / arweave_gateway
>   / turbo_endpoint / archive_namespace`; passphrase + wallet routed to `~/.lbrain/env` (600).
> - `cli.py` — `archive`, `recall`, `retrieve --txid`, `shred --txid`, `archives`.
> - `mcp_server.py` — `lair_deep_recall` tool.
> - `tests/test_archive.py` — 9 tests (crypto round-trip, tamper/wrong-pass, byte-identical
>   archive→retrieve, snapshot faithfulness, crypto-shred undecryptable, content-addressing).
>
> **Use it:** `export LBRAIN_ARCHIVE_PASSPHRASE=…` then `lbrain archive --from-file SESSION.md`
> → `lbrain recall "<query>"` → `lbrain retrieve --txid <txid>` → `lbrain shred --txid <txid>`.
> Enable real permaweb: set `arweave_enabled = true`, `arweave_transport = "arweave"`,
> `arweave_wallet_path` in config (or `LBRAIN_ARWEAVE_WALLET` in env), `pip install -e .[arweave]`.
>
> **Deferred to v2 (unchanged from below):** ar.io Turbo free-<100KiB ANS-104 bundled uploads
> (needs data-item signing the L1 client doesn't expose); snapshot-also-on-Arweave; Studio
> binary selectivity; surfacing archives in the main `query` path.

---


**Audience:** a fresh agent session building the Arweave archival layer for LBrain, with no
prior context. Self-contained. Read fully before coding. This is **Track B** (Arweave),
running in parallel with Track A (the elicitation research, owned elsewhere).

---

## 0. The one idea that must survive implementation
We are not building "permanent memory" — we are building **VERIFIABLE memory.** A model that
"remembers" reconstructs (lossy, confabulation-prone). Retrieval from an immutable,
tamper-evident record is a *provable* claim. That is the entire differentiator. Every design
choice below serves it. (Same thesis as the company: verifiable reasoning, verifiable assets,
now verifiable memory.)

## 1. Architecture — two tiers
- **Tier 1 (EXISTS — LBrain):** retrieval/curation. sqlite-vec + FTS5, Gemini embeddings,
  RRF fusion, rerank, Hebbian graph, consolidation summaries, AMP gating/budget/provenance,
  core memory, supersession. Markdown source-of-truth. This is "selectivity at the surface."
- **Tier 2 (BUILD THIS):** permanent encrypted archive on Arweave. "Permanence at the substrate."

**The decided storage policy (do not deviate): STORE FULL, READ SNAPSHOT.**
Compress at retrieval, NEVER at storage. Reasons: storage is cheap (text session = KB,
pennies forever); the scarce resource is the context window at inference; and storing only a
distilled slice destroys the ground truth → kills verifiability (a summary you can't audit
against the original is just another hallucination) and forecloses re-summarizing later with
better models. So:
- Write the **full encrypted session** → Arweave (the book on the shelf).
- Write a **structured-bullet snapshot (~20-30%)** → also Arweave, *referencing* the full tx,
  and mirror it + its embedding in LBrain's index (the card-catalog entry).
- Retrieval reads the snapshot (fast/cheap). Deep-recall / audit fetches + decrypts the full
  session by txid.
- Archive **periodically** (per-session / per-checkpoint, ANS-104 batched) — not per-token.

## 2. The accelerator — reuse, don't reinvent
**MemoryPort (`github.com/t8/memoryport`, Apache-2.0) has a WORKING Arweave stack** in Rust
(`crates/uc-arweave`): wallet signing, ar.io **Turbo** ANS-104 bundled uploads, **x402 USDC**
pay-per-upload, GraphQL tag-queries for rebuilds, fetch-by-txid, and **crypto-shred** deletion
(destroy the key = erase permanent ciphertext). Clone it, read `uc-arweave/` and the
`TECHNICAL_SPEC.md`. **Study/adapt the patterns** — the hard part is already open-sourced.
- LBrain is Python; MemoryPort is Rust. Pick the integration path by effort:
  (a) **Python → ar.io Turbo HTTP API directly** (likely simplest: ANS-104 data-items via
  the Turbo endpoint `https://turbo.ardrive.io`, GraphQL via an ar.io gateway), or
  (b) a small Rust sidecar / FFI reusing uc-arweave, or (c) `arweave-python-client`.
  Recommend (a) for the MVP.

## 3. Crypto + silo (non-negotiable)
- **Encryption:** AES-256-GCM, key derived via Argon2id from a user passphrase. Encrypt
  BEFORE upload. The key NEVER touches Arweave and NEVER goes in `config.toml` — use the
  existing secret pattern: `lbrain/config.py` `_write_env_var()` → `~/.lbrain/env` (chmod 600).
- **Crypto-shred = logical delete:** destroy the key → the permanent ciphertext is
  unrecoverable. This is how "permanent but erasable" works (persistence tiering).
  - **HARD shred (default, shipped):** `lbrain shred` destroys the key AND purges the local
    ~20-30% cleartext snapshot + its FTS/vector rows, leaving only an audit stub (txid, title,
    dates, flag). `--soft` keeps the snapshot for browsing (payload still unrecoverable).
  - **Caveat (document for users):** crypto-shred only holds if `~/.lbrain/keys/` is NOT
    backed up elsewhere (cloud sync / Time Machine). A backed-up wrapped key + the passphrase
    still recovers the payload. Treat the keystore as the single sensitive root.
- **Silo the DATA, share the TECH.** Two namespaces, one engine:
  - *private working/research memory* (conversations, Lairs, strategy, IP) → encrypted,
    sovereign, crypto-shreddable, default-private.
  - *Studio creative provenance* (later) → permanent, verifiable, often public.
  Different keys / access / defaults; same archival engine. Do NOT duplicate infrastructure.

## 4. Where it plugs into LBrain (concrete)
- `lbrain/config.py` — add `arweave_enabled`, `arweave_wallet_path`, `turbo_endpoint`,
  `arweave_gateway`, archive-namespace; route the wallet/passphrase to `~/.lbrain/env` via the
  existing `_write_env_var` pattern (mirror how `gemini_api_key` is handled).
- `lbrain/store.py` — schema: add `arweave_txid` + `snapshot_txid` (per session/doc) so the
  index points into the permaweb; add a lookup by txid.
- NEW `lbrain/archive.py` — the Arweave writer/reader: `archive(session_bytes, tags) -> txid`,
  `fetch(txid) -> bytes`, encrypt/decrypt, snapshot generation (reuse `consolidate.py`'s
  summary machinery for the structured-bullet snapshot).
- `lbrain/cli.py` — wire archival into the commit/`remember` path (on-demand to start); add a
  `retrieve --txid` / deep-recall command; an `archive` command for batch backfill.
- MCP (`lbrain/mcp_server.py`) — optionally expose deep-recall by txid as a tool later.

## 5. Scope & sequencing (don't over-build)
- **MVP = OUR sessions, on-demand:** archive a session (encrypt→Arweave→store txid+snapshot),
  retrieve-on-demand by txid + via the snapshot in the index. Prove the loop end-to-end with a
  tiny session first (cost is pennies; verify a round-trip: write → fetch → decrypt → matches).
- **Cost note:** text = pennies, store it all. The Studio's IMAGES are heavy — when Studio
  integration comes (v2 paid-tier feature, NOT now), be selective on binaries (full-fidelity
  the keyframes/finals + the attestation log; reference, don't re-store, every intermediate).
- **Do NOT touch the Aeternum ship's critical path.** Keep this siloed for now.

## 6. Definition of done (MVP)
A real LBrain session can be: encrypted → written to Arweave (txid returned, tagged,
GraphQL-queryable) → its snapshot + embedding indexed → retrieved by semantic match (snapshot)
AND by txid (full, decrypted, byte-identical round-trip) → crypto-shred verified (key destroyed
⇒ undecryptable). All keys in `~/.lbrain/env` (600), nothing sensitive in `config.toml`.

## 7. Pointers
- Design rationale (authoritative): memory files `project-two-tier-verifiable-memory-2026-06-03`,
  `reference-memoryport-code-comparison-2026-06-03`, `project-lbrain-ao-substrate-direction-2026-06-01`,
  `project-lbrain-product-thesis-2026-06-01`.
- LBrain code: `lbrain/` (study `config.py` secret pattern, `store.py` schema, `consolidate.py`
  for snapshot reuse, `amp.py` for the injection layer).
- MemoryPort blueprint: `git clone https://github.com/t8/memoryport` → `crates/uc-arweave/`.
