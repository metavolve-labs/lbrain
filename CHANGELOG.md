# Changelog

## 0.1.4 — 2026-08-09

### Added — the dial-in: agent-led one-time setup (`lbrain setup`)

Your agent interviews you (nine defaulted questions — sources, embeddings,
recall-first hook, auto re-sync, memory placement, core memory, MCP, secret
hygiene, history import) and performs only the additive steps, recording each
in `~/.lbrain/setup-manifest.md` with its undo command. `lbrain setup
templates` writes the hook scripts (recall-first is warn-by-default;
`LBRAIN_FIRST_MODE=block` opts into gating). `lbrain setup status` lists what
was installed and flags drift; `lbrain doctor` reports the same drift. The
interview says plainly: a large imported corpus is served as-is — the engine
dates and supersedes, it cannot know which old claims are still true. Vet
high-consequence records; `lbrain stale` is the audit tool.

### Added — operator-signed authority records for `gcx://` resolution (opt-in)

A squatted or contested name can now be resolved by an operator-signed
authority record: `GCX-Authority` tag query, signature-holder selection,
supersession by greatest confirmed block height, refusal on ties, unconfirmed
ordering, and dangling pointers. Additive by design — a name with no authority
record resolves exactly as before. Covered by U.S. Provisional 64/128,651
(filed 2026-08-09). Local verification against the specification-pinned
operator key (design in `docs/DESIGN-gcx-authority-verification.md`) lands in
a follow-up.

### Changed — refusal moved into the resolver (G2)

`Resolved.content` now raises unless the record verified; deliberate handling
of unverified bytes must name `raw_content`. The CLI refuses to write or pipe
unverified bytes and withholds previews of content that failed verification.

### Added — session-scoped core-memory serving (opt-in, default off)

Config-gated; with the gate off, serving is byte-for-byte identical to 0.1.3.

### Fixed — a chunk carried the heading it STARTED on, not the ones it lived under

Found in live use, not by inspection. A document titled
`# RFC full-corpus mint — ✅ EXECUTED + VERIFIED 2026-07-25` splits into H2
sections, and the splitter discarded the H1. So the section holding a superseded
count reached the ranker as:

```
[1] binds      "Current corpus = 8,871 RFCs numbered 1000–9999 only"   ← stale
[2] near-miss  "DONE. 9,791 RFCs minted (+15 pilot = 9,806)"           ← correct
```

**The admissibility gate admitted the stale figure and rejected the correct one**
— and on the evidence it had, it was right to. The stale chunk began with
`## Step 1 — COMPLETE the corpus (BLOCKER — do not skip)` and contained nothing
saying the work had finished, or when. Supersession, honest dating and the gate
all missed it, because all three read a chunk that had been stripped of the one
line that dated and closed it.

Chunks now carry `heading_path` — the ancestor headings above them — which
reaches the embedding, the FTS row, and claim-date extraction. Continuation
chunks of an oversized section also carry that section's own heading, so a table
row split onto a later chunk still names where it came from.

**Cost, stated plainly:** `CHUNKER_VERSION` 2 → 3. Chunk *boundaries* are
unchanged, but any corpus with H2 sections under an H1 re-chunks and **re-embeds**
on next import — on one live 31-document corpus that was 293 of 325 chunks (90%).
A flat or single-heading corpus hashes identically and does not move.

**Narrowed, not closed:** a date asserted in an ancestor *heading* now reaches a
deep chunk. `**Last Updated**:` and frontmatter `date:` live in the document
header block, which is not a heading — those are still visible only to the
leading chunk.

### Fixed — `doctor` gave a v2 index under v3 code a clean bill of health

`doctor` printed the embedding fingerprint (`✓ stored vectors match the live
embedding config`) and said nothing at all about the chunker. The chunker guard
existed — 0.1.3 shipped it — but it lived only in `import`. So the command an
operator runs to ask *"is my index sound?"* answered yes about an index built by
code they no longer run.

That is the A-435 blind spot one layer up: the guard was added where the code
*acts* on drift and not where a person *looks* for it. `doctor` now reports
`CHUNKER DRIFT` with the stored and live fingerprints and the command to fix it,
and both commands derive that fingerprint from one shared implementation instead
of two copies that could drift apart and each look right alone.

Deliberately **not** part of `doctor`'s non-zero exit contract: that gate means
"the stored vectors cannot be trusted". Chunker drift is a weaker claim — stale,
not wrong — and `import` repairs it. Widening the exit code would start failing
every script that gates on `doctor`, to report something the next import fixes.

### Added — frontmatter `date:` is a claim-date tier (#7)

A portable claim date that rides inside the file, so a corpus copied between
machines no longer reages every file to its ingestion date.

### Fixed — an unprovisioned brain said the wrong thing

MCP reported a missing optional extra when the real problem was that the brain
had never been provisioned.

### Docs / CI

Security reports point at the live, delivery-proven contact form; the contact
domain is explained rather than left looking like phishing. The release workflow
is dispatch-only with guards that can actually fire.

## 0.1.3 — 2026-08-02 — fail-closed provider, macOS install, Trusted Publishing

**This entry was reconstructed on 2026-08-03.** 0.1.3 shipped to PyPI with no
changelog entry at all — the file jumped 0.1.2 → nothing while a release went
public. Recording that plainly rather than backdating it silently, because a
changelog that quietly grows entries is worth less than one that says when it
was written.

### Fixed — `lbrain init` died on stock macOS Python

An `AttributeError` on Apple's system Python. This was the launch blocker, and it
could only have been found on a real Mac — WSL Python always carries the
`enable_load_extension` symbol, so every existing test environment passed.

### Fixed — the provider now fails closed

A provider typo was raising a stack trace instead of being reported as what it
is: a config fault. Internal identifiers were also removed from shipped source.

### Fixed — retrieval and onboarding papercuts

- `lbrain serve` printed `type=?` for **every** record on a plain-markdown corpus.
- `lbrain init`'s failure note told the user to run the command that had just failed.
- `.gitignore`'s `~/.lbrain/` pattern never matched anything.
- `AGENTS.md` served Gemini-native guidance as current doctrine.
- `check-action`, the mistake-prevention tool, was inert — 1 of 8 rules live, now 5 of 8 (A-438).

### Fixed — disclosure seam (CRITICAL)

`check_action` bypassed disclosure scope through the MCP path. Also: an
abstraction is synthesis, not an artifact, and was being classified as one.

### Added — release integrity

Trusted Publishing (OIDC) — no stored upload credential. Tests refuse to run at
all against a real install, and isolation now covers every install path.

## 0.1.2 — 2026-07-30 — Windows ranking, the knowledge graph, and consent

**Upgrade from 0.1.1 if you are on Windows.** Three ranking features were silently
degraded there; nothing errored, results were just quietly worse.

### Fixed — Windows ranking was silently degraded
The 0.1.1 Windows fixes stopped the crashes but missed three more instances of the
same path-separator bug. Slugs were derived by splitting on `/` only, so on Windows:
- the **wikilink graph boost** never matched,
- the **supersession de-rank** never matched,
- and **claim-date extraction** searched the whole path, letting a dated *parent
  directory* stamp its date onto every file inside it as that file's claim date —
  a false freshness signal from the function whose only job is honest dating.

### Fixed — the knowledge graph resolved 36% of its links
Three separate causes, found in that order:
1. Every lair is `<DIR>/LAIR.md`, so filename-derived slugs collapsed **164 of 167**
   lairs onto the single slug `LAIR`. A lair's identity is its directory.
2. Fixing that moved resolution 35% → 36%, which is how the real cause was found
   rather than assumed.
3. Wikilinks are written as relative paths (`[[../../some-dir/LAIR]]`) and were
   compared literally against bare slugs. Both sides now normalize to one slug space.

**Live corpus: 36% → 99% of wikilink targets resolve.** Applied on read, so existing
brains are fixed with no re-import.

### Fixed — supersession could be recorded backwards
`**Supersedes**: nothing · **Superseded by**: [[X]]` captured the *second* clause and
wrote the edge inverted — registering a document as superseding the thing that
replaced it. An inverted edge is worse than a missing one: it buries the live record
and promotes the dead one. Capture now stops at a `·`/`|` separator, and an explicit
`nothing`/`none` is honoured.

### Fixed — frontmatter edits were invisible to `import`
Change detection hashed the body only, so editing `type:`, `description:` or
`verify_by:` never took effect and the old value persisted indefinitely. Detection is
now separate from the body hash, and repair is a one-row update — a metadata edit
changes no chunk, so nothing is re-embedded. Reported as `meta-refreshed: N`.

### Added — `lbrain resolve` and a `gcx://` MCP resource
`lbrain resolve gcx://rfc/793` fetches a record and verifies it against a SHA-256
written **on-chain at mint time** — the hash comes from the chain, not from this
package or our servers. The same is exposed as an MCP *resource*, which refuses to
return content on hash mismatch or when no hash was recorded.

### Added — `lbrain whoami` / `lair_whoami`
What this memory is and what it is trusted for: identity, what is indexed, and what
the serving format does and does not guarantee. An unregistered brain is a normal,
fully-functional state.

### Changed — the model download now asks
The on-device path fetches a ~67 MB model on first use and previously said nothing
beforehand. `init` now explains what it is — the model coming *down*, not your
documents going *up* — names the cache location, offers the hosted alternative, and
asks. `--yes` skips it; a non-interactive session prints the notice and proceeds
rather than hanging a pipeline.

### Also
- `SUPERSEDED` now displays on keyword search, not only on ranked search.
- Retrieval timing uses a monotonic clock; a system clock adjustment previously
  produced negative durations.
- `docs/KEYS.md` rewritten — it opened by claiming LBrain needs an embedding API
  (it does not) and never mentioned the on-device default.
- Issue templates that ask for `lbrain doctor --json`, and a known-issues table.

156 tests.

## 0.1.1 — 2026-07-28 — security + Windows. **Upgrade from 0.1.0.**

**0.1.0 is yanked.** It is broken on Windows and contradicts its own privacy
claim. If you pinned it, unpin.

### Fixed — every native-Windows install was bricked by `lbrain init`
- `config.toml` was built by raw f-string interpolation, so a Windows path
  (`C:\Users\...`) emitted an invalid `\U` escape. `init` reported success and
  every later command died in `Config.load()` with `TOMLDecodeError`.
- The onboarding templates contain non-ASCII characters that do not encode in
  cp1252, which Windows uses when `encoding=` is omitted — so `init` raised
  `UnicodeEncodeError` while scaffolding lairs, *before* reaching the TOML bug.
  All text I/O is now explicit UTF-8.
- The `000-PRIORITY` ranking boost silently never fired on Windows: the path was
  split on `/` only. Not a crash — a ranking difference with no error message.

### Fixed — an ambient API key is not consent to send your corpus away
- With no `config.toml`, the provider defaulted to a hosted embedder and adopted
  `OPENAI_API_KEY` / `GEMINI_API_KEY` from the environment, so `lbrain import &&
  lbrain embed --stale` could ship your corpus to a third party on a key you
  never pointed at LBrain. Unconfigured now means **on-device**.
- `lbrain archive` selected the OpenAI key whenever the provider was not
  `"gemini"` — including `"local"` — and POSTed the raw session transcript to
  `api.openai.com`. Only an explicitly named hosted provider can now leave the
  machine, and the fallback is no longer silent.

### Fixed — security
- `lair_check_action` returned retrieved corpus text with no untrusted-data
  notice, no fence and no sanitization, while presenting it as rules to an agent
  that calls it *before* irreversible actions. A planted note was a direct
  agent-hijack primitive. Now carries the same containment as `lair_query`.
- The legacy prose serving path used a weaker fence than the structured path;
  both now share one hardened implementation. The CLI had no containment at all.
- `brain.db` — the entire corpus in cleartext — was created world-readable
  (0644 in a 0755 directory). Now 0600 in a 0700 directory.
- Import followed `*.md` symlinks out of the corpus root, so a cloned repo could
  choose which of your files got indexed and served.

### Changed
- `serve_mode` now defaults to `structured` for new installs (measured 5/8 → 8/8
  answer presence). Rollback: `serve_mode = "prose"`.
- Records asserting an open state are annotated at serve time
  (`unverified 27d`), so a claim with a shelf life says so where it is used.
  Rollback: `serve_staleness = false`.
- Pre-change backup trees are no longer indexed, and docs that become
  unindexable are pruned — superseded copies were ranking against the records
  that corrected them.

Security findings adjudicated by hand against live code; 123 tests.

## 2026-06-08 — Archive extracted to an optional subpackage

Finished the "second product sharing the repo" loose end from the polish pass: the
Tier-2 encrypted Arweave archive is now a self-contained optional subpackage,
`lbrain/archive/`, with a strict one-way dependency (archive → core, never the reverse).
The core retrieval engine (index → embed → store → search → MCP) no longer knows the
archive exists. **No data migration** — existing `brain.db` archive tables are reused
as-is (verified live: `lbrain recall` deep-recalls real 7.9 MB records). 27/27 tests pass.

### Structure
- New subpackage `lbrain/archive/` (1,372 lines): `archiver.py` (moved from
  `lbrain/archive.py`), `crypto.py` (moved from `lbrain/crypto.py`), `storage.py`
  (the archive tables + queries, extracted from core `store.py` as `ArchiveStore`),
  `cli.py` (the 7 archive commands + a `register(main)` hook), `mcp.py`
  (`lair_deep_recall` + a `register(mcp)` hook), `config.py` (passphrase resolution,
  moved out of core config).
- Core shrank to 2,742 lines. `store.py` lost the `archives`/`vec_archives`/
  `fts_archives` schema and 7 archive methods; `stats()` and `reset_vectors()` now
  tolerate the archive layer being absent (table-existence guarded). `cli.py` and
  `mcp_server.py` register the archive surface only via a guarded `try/except ImportError`.
  The prompt-injection fence helpers moved to `amp.py` (`amp.fence` / `amp.UNTRUSTED_NOTICE`)
  so both core and archive share one definition.

### Genuinely optional
- `cryptography` moved from a core dependency to the `archive` extra — it gates the whole
  subsystem. `pip install lbrain` → lean core, no `cryptography`, archive commands/tool
  absent. `pip install lbrain[archive]` → encrypted local archive. `pip install
  lbrain[arweave]` → + real permaweb writes. **Verified:** with `cryptography` blocked,
  the core CLI loads with 14 commands and the archive surface correctly does not register.

### Accepted compromise
- The `arweave_*` / `archive_namespace` fields stay on the core `Config` dataclass
  (passive data, so `Config.write` round-trips them — moving them out would let
  `add-source` silently drop a user's archive config). No core *logic* branches on them.

---

## 2026-06-07 — The Polish Pass (significant revision)

A systematic cleanup: cut the overengineered, default-OFF "brain-metaphor" layers,
removed the behavioral-bias injection that was confusing the agent, hardened the
crypto/secret handling, and fenced retrieved text against prompt injection. **No loss
of day-to-day behavior** — every removed retrieval feature shipped disabled by default.

Package shrank **4,575 → 3,941 lines** (−634), and **−635 net** in the diff
(750 deletions / 115 insertions). Two modules deleted. 27/27 tests pass.

### Removed — conflict / agent-bias
- **Cognitive Nutrition substrate primers** (`lair_protocol.py`). These injected
  hardcoded, project-specific directives ("prefer our substrate", "Cloud Run for
  stateless services", GCP project ids…) ahead of search results on keyword triggers —
  biasing the agent toward opinions the user never stored. This was the "something
  slightly off." Memory now surfaces what's saved; it does not editorialize.
  - Dropped: `SUBSTRATE_PRIMERS`, `PRIMER_TRIGGERS`, `cognitive_nutrition_preamble()`,
    the `--no-prime` CLI flag, and the call sites in `mcp_server.py` / `cli.py`.
  - **Kept:** `should_commit_to_lair()` (commit-check) and `detect_anti_pattern()`
    (feedback guard) — the genuinely useful, content-neutral heuristics.

### Removed — overengineering (all were default-OFF; zero behavior change)
- **Hebbian co-retrieval + spreading activation** (Tier 2b): `search.py` step 7, the
  `associations` table + indexes, and `store.strengthen_associations/neighbors/
  representative_chunk`. Injected docs that never matched the query — a precision risk
  with unproven value over RRF.
- **Temporal decay / salience** (Tier 2a): `search.py` step 6 + reinforce-on-use, the
  `last_retrieved`/`retrieval_count` columns, and `store.record_retrievals`. A ±0.15
  nudge that barely moved ranking while adding a write per query.
- **Cross-encoder rerank** (Tier 2c): deleted `rerank.py`. Required an uninstalled
  optional dep, so it was **always a no-op as shipped**; `available()` was dead code.
- **Consolidation "heartbeat"** (Tier 3): deleted `consolidate.py`; removed the
  `summaries`/`vec_summaries`/`fts_summaries` tables, all summary methods in
  `store.py`, the summary-injection block in `search.py`, and the `consolidate` /
  `summaries` CLI commands. It pulled in **undeclared `sklearn` + `numpy`** (would
  crash on use) for marginal payoff over RRF-ranked results.
- Net effect: `search()` collapsed from a 9-stage pipeline to its lean core —
  vector + BM25 → **RRF fusion** → priority boost → wikilink graph boost →
  supersession de-ranking. ~280 → ~120 lines.

### Removed — dead code / config bloat
- `crypto.rewrap_key()` (never called).
- Config knobs with no readers: `wikilink_boost` (the boost is hardcoded in
  `search.py`), `bm25_weight`, `vector_weight` (unused since RRF), plus all the
  knobs for the cut features above. `config.py` lost ~16 fields across the dataclass,
  `load()`, and `write()`.
- Unused `EmbedClient` import in `cli.py`; unused `struct` import in `search.py`.
- **`rank-bm25` dependency** dropped from `pyproject.toml` (BM25 comes from FTS5; the
  package was referenced only by the now-deleted `bm25_weight`).

### Security hardening
- **Prompt-injection containment (PI-1, HIGH):** retrieved note/snapshot text returned
  by `lair_query` / `lair_search` / `lair_deep_recall` and the always-on core-memory
  block is now wrapped in a `⟪note⟫…⟪/note⟫` fence (with the sentinel neutralized in
  content so planted text can't forge a boundary) and prefixed with a standing notice
  telling the agent to treat fenced content as data, never instructions.
- **Atomic secret write (H1, HIGH):** `config._write_env_var` now writes `~/.lbrain/env`
  via an `O_CREAT` 0600 fd to a same-dir temp file + atomic `os.replace`, and chmods
  `~/.lbrain` itself to 700. Closes the TOCTOU window where the secret briefly existed
  world-readable under the process umask before the old post-hoc chmod.
- **Argon2 parameter clamp (M3, MED):** `crypto._unwrap_dek` now rejects out-of-bounds
  KDF params (time ≤ 16, memory ≤ 2 GiB, lanes ≤ 16) from the portable/backup-able key
  envelope, preventing a tampered `.key` file from triggering a multi-TB alloc (OOM DoS).
- **Honest crypto-shred docs (M2, MED):** corrected the "UNRECOVERABLE" overstatement —
  the in-place overwrite is a no-op on CoW/SSD filesystems and doesn't defeat
  snapshots/backups; the real boundary is the key `unlink`. Docstrings + the `shred`
  confirmation prompt now say so.

### Docs
- `README.md` updated to remove Cognitive Nutrition framing and reflect the lean
  retrieval pipeline, the core-memory + AMP layer, and the prompt-injection containment.

### Intentionally kept
- **Tier-2 encrypted Arweave archive** (`archive.py`, `crypto.py`, the archive CLI +
  `lair_deep_recall`). It's a working subsystem with the strongest crypto in the repo
  and its own test suite. It is effectively a *second product* sharing the repo;
  recommended future step is to split it into an optional package, but it was **not**
  removed in this pass. (Its security findings above were applied.)
- `contextual_prefix` (cheap, integrated), `supersede_aware`, the AMP gating/budget/
  provenance layer, and the always-on core-memory block (curated, user-authored).

### Note for operators
A long-running MCP server process keeps the pre-revision code in memory until
restarted — restart it (or the Claude Code session) to pick up the prompt-injection
fence and the removed primers. Existing `brain.db` files keep their now-unused
columns/tables (`associations`, `summaries`, `last_retrieved`, `retrieval_count`);
they're harmless and ignored. Fresh DBs no longer create them.
