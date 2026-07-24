# Design — Binding-Aware Serving (serve-path upgrade, 2026-07-24) · v2 post-red-team

**Status:** v2 — corrected after a 3-lens adversarial design review (evidence/doctrine, API/consumers,
security/injection; 2 CRITICAL + 9 MAJOR findings adopted; review record in the 2026-07-24 session).
**Owner:** Metavolve Labs engineering.

## Evidence base (post-v3b honest calibration — what this is and is NOT justified by)

- **Extraction/utility collapse (REAL):** blur collapses a model's ability to extract a *present*
  value: 26–64% C-utility under attribution-blur vs ~100% clean (Llama/Qwen, directly measured) and
  ~80% clean on Opus — structure roughly *doubles* utility. Nudge-independent where re-tested
  (2 models, v3b) and consistent across the 10-model panel. (RESULTS-RUN2-3, final standardized map.)
- **Dogfooding (2026-07-23/24 marathon):** retrieval returned the exact provenance line in ~1s where
  grep/find burned cycles and context.
- **NOT claimed:** "prevents confabulation" — v3b killed blur→spontaneous-confab (prompt-nudge
  artifact; models misattributed 0–1% without an explicit invitation). Near-miss records are NOT
  claimed to induce misattribution; the real-corpus 18% residual's mechanism is an open question.
  U2's justification is serve-quality + utility + honesty (flag ambiguity, never assert past it) —
  not confab prevention. `admissibility.py`'s docstring predates v3b and is corrected in this change.

## Current serve path (verified live 2026-07-24)

`lair_query` → `search()` (ranking healthy) → `amp.budget()` → `h.text.strip().replace("\n"," ")[:360]`
— newlines melted, mid-token cuts, blind chunk PREFIX served (can miss the matched region), title/path
emitted unsanitized outside the fence. Chunks are stored structure-intact; melting is serve-time only.
Live scale: 1,919 docs / 9,476 chunks.

## The four upgrades

### U1 — Serve structure, not prose

Per-hit **attribution-bound record block**:

```
[1] ★ <title>                                        ← sanitized (see Sanitizer)
    src: <rel_path> · chunk <idx> · type=<doc_type> · <date-label> <YYYY-MM-DD> [· SUPERSEDED] [· abstraction] [· binds|near-miss]
⟪note⟫
│ <line-preserving excerpt — every fenced line carries the "│ " prefix>
│ <whole lines, window centered on query-relevant lines>
⟪/note⟫
```

- **Excerpt windowing (fully specified):** tokenize the query with `admissibility._terms` (shared
  normalization, so U1 and U2 agree on "query-relevant"). Score each line by query-term hits; choose
  the contiguous whole-line window with maximal term score fitting `serve_chunk_chars`; ties → earliest
  window. **Zero-density fallback** (pure-vector hits with no literal overlap): chunk-prefix lines —
  legacy-equivalent, strictly no worse than today. **Chunk ≤ budget → serve whole chunk verbatim.**
  **Single-line overflow** (measured: 246 live lines > 700 chars): hard-cut at budget on a word
  boundary centered on the line's densest term region, with an explicit `…` elision marker — bounded
  output even for newline-free chunks.
- **Per-line fence prefix `│ `:** no fenced line can match header grammar at column 0, and every
  line self-declares as fenced content even mid-block (multi-line fence-state tracking hardening).
  Parsers/consumers must ignore fenced (`│ `-prefixed) lines when reading header grammar.
- **Honest dating** (red-team: mtime is NOT claim age): filename date (`YYYY-MM-DD` in basename,
  the corpus convention for claim dates) → `dated 2026-07-23`; abstractions → `generated <mtime>`
  (mtime = synthesis time by definition); otherwise `file-dated <mtime>` — named for what it is.
- **No-mutation invariant:** `Hit.text` always carries the full chunk text; excerpting is a pure
  render-time function. `search.py` changes are limited to adding `d.mtime` to `keyword_only`'s
  SELECT (additive). `lair_check_action`/`detect_anti_pattern` see full text, unchanged.

### U2 — Ambiguity annotation + gate (wire admissibility rung 1)

- Question-shaped queries only (trailing `?` or interrogative opener): run `admissibility.judge`
  **on the exact excerpt being served** — every annotation describes content the consumer can see
  (red-team API-C2). Deterministic, no LLM, ~0ms.
- Header annotation: `· binds` (ADMISSIBLE) / `· near-miss` (INADMISSIBLE_NEAR) / nothing.
- **Gate:** density computed over the **post-budget kept set** (the records actually served),
  comparison `≥`, denominator = all kept records. Fires when `near_count ≥ gate_min_near` AND
  `near_count/kept ≥ gate_density` → prepend a fixed-text binding notice (count interpolation only;
  no corpus text can reach it). At defaults the density term dominates; `gate_min_near` is a floor
  for small served sets.
- **Non-destructive:** flags, never withholds, never reorders (withholding = a cut → measure first).

### U3 — Binding table (deterministic v1, hazard-corrected)

- Fires only when U2's gate fires AND `qkind ∈ {quantity, date}` — **asserted in code**; identity
  questions NEVER build a table (ID_CAND matches free text — load-bearing exclusion, tested).
- **Rows come ONLY from ADMISSIBLE-verdict records** (red-team CRITICAL: NEAR records return
  bound candidates too — the '924 for 780' trap; a NEAR row would condense the exact wrong-entity
  hazard into the most salient position). NEAR values are never tabled.
- Label honestly: `possible bindings (heuristic extraction — verify in the records below)`, not
  "the answer".
- Row: `<value> ← <title> (<date-label> <date>)`. Value re-validated against strict numeric/date
  shapes (month-words require an adjacent digit; bounded length; noise dropped); title/date pass the
  same sanitizer as headers. ≤3 values/record, ≤10 rows.

### U4 — Per-consumer tuning + measured default

- `serve_mode`: per-call param on `lair_query` + CLI `--mode`, config default. Values:
  `structured` | `prose`. Unrecognized value → **fall back to prose + warning** (fail-open to legacy).
- **Code default = `prose`** (red-team doctrine finding: default-value ≠ configured-on ≠
  measured-useful). Zero behavior change for unconfigured installs. The live brain flips to
  structured via config **only after the answer-presence A/B on real corpus queries** (this session's
  verification plan), with the measurement recorded.
- `prose` mode keeps the **legacy rendering pipeline untouched** regardless of every other new knob
  (serve_admissibility etc. are ignored in prose). One deliberate exception to byte-parity: the
  shared `UNTRUSTED_NOTICE` constant was updated in BOTH modes (security posture item 7) — the
  prose pipeline emits the new constant. Scoped here explicitly so "no change" is never claimed
  past what was verified (Doctrine #3).
- `lair_search`/CLI `search` follow `cfg.serve_mode` for rendering; no admissibility (keyword search
  stays lean). Consumers inventoried: heartbeat hook + CLI wrappers see no change until the config
  flip, which is a deliberate, logged act.
- New knobs: `serve_mode="prose"`, `serve_chunk_chars=700`, `serve_admissibility=true`,
  `gate_min_near=3`, `gate_density=0.5`.

## Budget accounting (specified)

- `amp_budget_chars` bounds the **fully rendered record blocks** (header + fence + prefixed excerpt):
  render first, then keep the score-ordered prefix of records that fits; always ≥1. At live defaults
  (6000, k=8, 700-char excerpts) this serves **~6–7 records instead of 8** — accepted deliberately:
  the records served extract better; raising the budget is a config decision, not a silent one.
- Response-level additions (untrusted notice, gate notice, binding table) are bounded (≤ ~1KB, rows
  capped) and reported in the provenance footer (`· notices N chars`) alongside record usage;
  `amp_provenance = false` suppresses the footer in structured mode too.
- Excerpt windowing is O(n log n) via prefix sums (diff-review MAJOR: the naive per-start rescan was
  O(n·budget) — ~1.2s on a 16K-line newline-dense chunk, ~9.5s at k=8, a serve-path DoS); the
  single-line cut centers on the line's densest term region (two-pointer over match positions,
  ties → earliest), and excerpt length never exceeds the budget, elision chars included.

## Security posture (honest statement — red-team corrected)

The fence protects **body text**. A small enumerated set of corpus-derived fields renders OUTSIDE
the fence — **title, rel_path, doc_type, binding-table values/titles/dates** — and for those the
**sanitizer is the load-bearing control**:

1. All line separators collapse to spaces: `\r \n \x0b \x0c \x85 U+2028 U+2029` (the codebase's
   old `.replace("\n"," ")` idiom is insufficient — tested per separator).
2. Control chars stripped (incl. ANSI ESC — no terminal escape injection).
3. Fence sentinels AND homoglyph doubles neutralized (`⟪⟫ 《》 ⧼⧽` → safe forms). Residual risk:
   the homoglyph space is open-ended; the per-line `│ ` prefix is the second layer (a forged
   "fence close" still leaves following lines visibly prefixed).
4. The header separator `·` (U+00B7) AND its confusable dot/bullet set (U+0387 ano teleia, U+2027,
   U+30FB, U+2022, U+2219, U+22C5, U+16EB) are replaced in corpus-derived fields, after NFKC
   folding — a hostile FILENAME or title cannot forge header grammar or the `· binds` trust
   annotation through the literal char or its homoglyphs (diff-review MAJOR: U+0387 bypassed the
   single-codepoint map). The code-generated `★` priority marker is likewise stripped from fields.
5. `doc_type` whitelisted to the known enum (`user|feedback|project|reference|abstraction`) else `?`.
6. Length caps on codepoint boundaries.
7. `UNTRUSTED_NOTICE` updated: titles and table values are also retrieved data, not instructions.

DoS bounded: excerpts hard-capped even for newline-free chunks; table rows capped.

## Reversibility (Doctrine #7)

- Code default prose; live flip is one config line; per-call override both directions.
- **Config.write is lossy today** (verified: drops `abstraction_topk_cap`/`abstraction_recency_guard`
  it loads, plus unknown keys + comments incl. an operator authorization note). This change: (a) adds the
  missing abstraction_* lines AND the new serve_* keys to `write()`; (b) adds a write→load round-trip
  equality test over every dataclass field as a hard gate; (c) **the live config.toml is edited
  manually, never via cfg.write()** (snapshot first). Full comment-preserving writer = follow-up.
- No DB schema change, no ranking change: rank-invariance test asserts identical
  (rel_path, chunk_idx, score) AND byte-identical `Hit.text` across modes.

## Verification plan

1. Unit: excerpt windowing (centered / zero-overlap fallback / giant-line / fits-whole), sanitizer
   (each separator class, ANSI, homoglyphs, hostile filename with `·`, hostile doc_type, length cap),
   fence integrity (forged header + forged `binds` + forged fence-close inside a fenced excerpt —
   all neutralized by prefix+sanitizer), prose byte-identity, rank/text invariance, gate thresholds
   (incl. trimmed-set: 5 kept / 3 NEAR → fires), table (ADMISSIBLE-only; NEAR-with-candidates → zero
   rows; identity → no table even with ID-shaped injection strings; date-noise dropped), budget
   accounting, invalid serve_mode fallback, config round-trip.
2. Existing suite green.
3. Live read-only before/after on real queries (structured vs prose; same hits).
4. **Answer-presence A/B (measured-useful gate for the config flip):** N known-answer queries about
   the real corpus; metric = is the answer value present in the served output? structured must ≥
   prose before the live default flips.
5. Adversarial multi-lens diff review before commit.

## Explicitly out of scope (future, measurement-gated)

- LLM condensation on the read path; withholding/re-query routing on gate fire; per-model consumer
  profiles; index-time entity·attribute·value extraction; comment-preserving config writer.
