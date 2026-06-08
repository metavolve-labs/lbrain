# Changelog

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
