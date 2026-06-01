# The Self-Filling Memory Protocol (SFMP)

**Status**: PROPOSED SPEC | **Builds on**: `lair_protocol_check`, `consolidate` (already shipped) | **Target**: LBrain

An autonomous loop in which an LLM (a coding agent, or a headless `lbrain` agent) periodically decides what to remember, where it belongs, and whether to append, update, create, or do nothing — without ever silently overwriting source-of-truth.

---

## 0. Design axioms (derived from corpus governance)

1. **Source files are authoritative; LBrain is a derivative cache.** The loop writes `*.md` source files, then re-imports. It NEVER edits the vector store directly as the system of record.
2. **History grows forward, never overwrites.** New entries prepend to `MEMORY.md` top; supersession is explicit (`**Supersedes:**`), the old file is marked historical, not deleted; refinements tag `Nth-pass`.
3. **Faithfulness over fluency.** The writer compresses, attributes sources, and flags disagreements. It must never invent a fact to make a memory read cleanly.
4. **One concern per lair; memory is the journal, lairs are the canon.** Memory entries point INTO lairs for canonical detail; the loop only promotes journal → canon under stricter gates.
5. **Confidence-gated autonomy.** `lair_protocol_check` already returns `confidence` + `should_commit`. The loop's entire write-aggressiveness is a function of that scalar crossed with a write-class risk tier.

---

## 1. The two existing primitives (the foundation)

| Primitive | Input | Output | Role in SFMP |
|-----------|-------|--------|--------------|
| `lair_protocol_check(text)` | conversation span | `should_commit, confidence, suggested_type ∈ {user,feedback,project,reference}, suggested_slug, reasoning` | **The "is this worth remembering?" gate** — the front door of the loop. |
| `consolidate` (heartbeat) | clusters of chunks | dense, provenance-linked summary memories | **The "compress accreted journal into canon" pass** — the back-end janitor. |

SFMP is the **control plane that wires these together** and adds the missing middle: *routing* (where does this go?), *dedup* (does it already exist?), and *write-class arbitration* (append vs update vs create vs promote).

---

## 2. Triggers (when the loop fires)

| Trigger | Mechanism | Fires | Default write-aggressiveness |
|---------|-----------|-------|------------------------------|
| **T1 Session-end** | coding-agent `Stop` hook / `lbrain session-flush` | end of every interactive session | capture pass only (no consolidation) |
| **T2 Threshold** | running token/turn counter in-session | every N=40 turns or 25k new tokens | capture pass on the rolling window |
| **T3 Heartbeat (cron)** | `lbrain heartbeat` via cron | nightly 03:00 + biweekly audit day | full pass: capture + consolidate + staleness sweep |
| **T4 Explicit** | `lbrain remember "<text>"` / user says "remember this" | on demand | capture pass, confidence floor lowered |
| **T5 Pre-action** | before a risky outbound action | inline | `lair_check_action` only (read-side guard, no write) |

T1/T2 are cheap (capture). T3 is the expensive convergent pass that pays down debt.

---

## 3. The capture pass (T1/T2/T4)

```
window = last N turns (or full session on T1)
spans  = segment(window)            # by topic shift / decision boundary
for span in spans:
    r = lair_protocol_check(span)
    if not r.should_commit:                continue
    if r.confidence < FLOOR[trigger]:      queue_for_review(span, r); continue
    candidate = {text: span, type: r.suggested_type,
                 slug: r.suggested_slug, conf: r.confidence,
                 reasoning: r.reasoning, sources:[sessionId, turn-range]}
    route(candidate)                  # → §5 decision tree
```

`FLOOR`: T4 explicit = 0.30 (user asked, trust them), T1/T2 = 0.60, T3 = 0.55. Anything `should_commit=true` but below floor goes to the **review queue** (§7), never silently dropped.

---

## 4. Dedup-against-existing (run BEFORE deciding write-class)

This is the step the raw primitives lack and the one that prevents the corpus from filling with near-duplicates.

```
hits = lair_query(candidate.text, k=8)          # semantic neighbours
for h in hits:
    sim   = cosine(candidate, h)
    is_same_concern = (h.type == candidate.type) and slug_overlap(...)
```

| Condition | Verdict |
|-----------|---------|
| `sim ≥ 0.92` AND same file | **DUPLICATE** → drop (or bump `Last Updated` only) |
| `0.78 ≤ sim < 0.92`, same concern | **UPDATE target = h.file** (append a dated `Nth-pass` block / row) |
| `sim < 0.78` OR new concern | **CREATE candidate** (subject to create gate §6) |
| hit is a **lair** (canon) and candidate is `project`/`reference` | **POINTER**: write a memory that links into the lair, do not duplicate lair content |
| candidate contradicts a hit | **CONFLICT** → never overwrite; write new + set `**Supersedes:**`, mark old historical, flag in review queue |

The similarity bands are the load-bearing knob; they should be config (`sfmp.dedup.update_band = [0.78, 0.92]`).

---

## 5. The append-vs-update-vs-create decision tree

```
                         ┌─ DUPLICATE ──────────────→ touch Last Updated, stop
                         │
candidate ─ dedup(§4) ───┼─ UPDATE (existing file) ──→ APPEND-IN-PLACE
                         │                              • memory: add "Nth-pass" block,
                         │                                refresh description if rule changed
                         │                              • lair: add row to the right table
                         │                                (Current State / Key Files / Decisions),
                         │                                bump Last Updated, never rewrite prose
                         │
                         ├─ POINTER ───────────────────→ CREATE memory that [[wikilinks]] lair
                         │
                         ├─ CONFLICT ──────────────────→ CREATE-WITH-SUPERSEDE
                         │                              • new file, **Supersedes:** old
                         │                              • old gets "(now historical)" banner
                         │                              • ALWAYS enters review queue
                         │
                         └─ CREATE ─────────────────────→ §6 create gate
                                                          ├ type∈{user,feedback,project} → new memory/*.md
                                                          └ "new durable domain" signal     → propose new LAIR (gated)
```

**Write-class risk tiers** (governs how much autonomy each class gets):

| Class | Risk | Autonomy |
|-------|------|----------|
| MEMORY append (new `Nth-pass` block) | low | fully autonomous ≥ floor |
| MEMORY create (`project`/`reference`) | low-med | autonomous ≥ floor |
| MEMORY create (`feedback`/`user`) | **med** | autonomous ≥ 0.75, else review (these change future behaviour) |
| LAIR table-row append | med | autonomous ≥ 0.75 |
| LAIR section/prose edit | **high** | **always human-gated** |
| LAIR create | **high** | **always human-gated** (must pass one-sentence test, get cross-refs) |
| Any CONFLICT/Supersede | **high** | write allowed, but **always queued** for human confirm |

Rationale: `feedback`/`user` memories and any lair mutation change how the agent behaves and what it treats as canon — exactly the writes where a hallucinated memory is most damaging.

---

## 6. The create gate (lair creation = highest bar)

A new LAIR is only *proposed*, never auto-committed. The loop emits a draft `LAIR.md` from `LAIR_TEMPLATE.md` and parks it in `lairs/_proposed/`:

1. Passes **one-sentence mission test** — the writer must produce a single-sentence Mission or it's rejected.
2. No existing lair scores `sim ≥ 0.78` on the concern (else it's an UPDATE, not a CREATE).
3. Draft is template-conformant: front-loaded header, Current State table, Key Files, Decisions Log, **Related Lairs with proposed bidirectional cross-refs** (the loop must list the back-references it would add to sibling lairs).
4. A human runs `lbrain lair-promote _proposed/<slug>` which moves it, writes the back-refs, adds to `START_HERE.md` + the index, then re-syncs. **This step is never autonomous.**

---

## 7. Safety rails (the non-negotiables)

| Rail | Implementation |
|------|----------------|
| **Never overwrite source-of-truth** | All writes are append/prepend or new-file. Prose replacement in a lair is forbidden to the loop. CONFLICT → supersede-not-delete. |
| **Faithfulness over fluency** | Every auto-written body carries a `**Source:**` line with `sessionId` + turn-range; consolidate keeps provenance links. `lbrain verify-memory <file>` re-reads the cited span and asserts the claim is supported. |
| **Confidence thresholds** | Per-class floors (§5). Below floor ⇒ review queue, not write. |
| **Human-in-the-loop gates** | High-risk classes land in `lairs/_review/QUEUE.md` as a checklist; nothing in queue is embedded as canon until approved. |
| **Action guard reuse** | Before writing a `feedback` memory derived from a correction, run `lair_check_action` on the *proposed rule* to ensure it doesn't contradict an existing feedback rule. |
| **Idempotency / rate cap** | Each span hashed; same hash never committed twice. Max M autonomous writes per pass (default 10) — overflow → queue. |
| **Re-sync is mandatory & atomic** | Every pass ends with `lbrain import <dirs> && lbrain embed --stale`. If import fails, `.md` writes are kept (authoritative) but the pass is flagged dirty for retry. |
| **Staleness, not deletion** | The loop never deletes. The heartbeat staleness sweep only *flags* (30–60d) or *proposes archive* (60+d). |
| **Dry-run default** | New triggers ship `--dry-run` first: emit the diff + verdicts to `_review/`, commit nothing, until the operator trusts the verdicts. |

---

## 8. The heartbeat pass (T3) — capture + consolidate + sweep

```
1. capture pass over any unflushed windows           (§3)
2. consolidate:                                       (existing primitive)
     cluster recent project/reference chunks → dense provenance-linked summaries
     • output goes to MEMORY.md as a consolidated block w/ **Supersedes:** the dailies it absorbs
     • dailies marked historical, kept (history grows forward)
3. staleness sweep:                                   (read-only → queue)
     for each lair/memory: age = today − Last Updated
        30–60d → flag in _review/QUEUE.md
        60+d   → propose _archive/ move (+ name canonical replacement)
4. dedup-merge sweep:
     find sim≥0.92 cross-file pairs → propose merge into canonical, queue it
5. graph integrity:
     find lairs missing a ## Related Lairs section or with dangling cross-refs → queue
6. re-sync: lbrain import && lbrain embed --stale
7. emit pass report to memory/SFMP_LOG/SESSION_YYYY-MM-DD.md
```

Consolidate is the mechanism that keeps `MEMORY.md` from growing unboundedly: daily journal entries get rolled into denser summaries on the heartbeat.

---

## 9. Output formats the loop must emit (conform to existing schemas)

**Memory file** (`memory/<type>-<slug>-YYYY-MM-DD.md`) — frontmatter then spine:
```yaml
---
name: <slug>
description: <retrieval-optimized; for feedback = rule + corrective behavior>
metadata:
  type: user | feedback | project | reference
  originSessionId: <uuid>
  sfmp:
    generated: true
    confidence: 0.84
    trigger: heartbeat
    source_turns: "142-159"
    verified: true            # set by verify-memory
---
# <Title> (YYYY-MM-DD)
**Rule:** …            # feedback only
**Why:** …
**How to apply:** …
**Source:** session <uuid> turns 142–159
**Supersedes:** [[old-slug]]   # if conflict
Related: [[other-slug]]
```
Then prepend the `## <emoji> HEADLINE (date)` + dense gist + `Detail: [..](file.md)` block to the top of `MEMORY.md`.

**Lair edits**: row appends to the correct table only, plus bump `**Last Updated**`. Never touch the 500-line cap without splitting (loop refuses; queues a split proposal instead).

---

## 10. New commands / flags the protocol needs

```bash
# --- orchestration ---
lbrain heartbeat [--dry-run] [--since <ts>] [--max-writes N]
lbrain session-flush [--session <id>] [--dry-run]
lbrain remember "<text>" [--type auto|user|feedback|project|reference] [--force]

# --- the new middle layer (routing/dedup) ---
lbrain route <candidate.json>      # dedup(§4) + decision tree(§5) → verdict
lbrain dedup-scan [--band 0.78,0.92] [--fix]

# --- writers (append-only, schema-aware) ---
lbrain mem-append <file> --block <text> --pass N
lbrain mem-create --type T --slug S --body <text> [--supersedes <slug>]
lbrain lair-row <lair> --table {state|keyfiles|decisions} --row "<md row>"
lbrain lair-propose --slug S            # emits template draft → lairs/_proposed/
lbrain lair-promote _proposed/<slug>    # HUMAN-ONLY: graph-link + index + resync

# --- safety / governance ---
lbrain verify-memory <file>      # re-reads cited source span, asserts faithfulness
lbrain review                    # opens lairs/_review/QUEUE.md (the HITL gate)
lbrain review approve|reject <id>
lbrain staleness-sweep [--flag-only]
```

**Config block** (`lbrain.toml`):
```toml
[sfmp]
floor.session_end = 0.60
floor.heartbeat   = 0.55
floor.explicit    = 0.30
autonomy.feedback_user = 0.75   # below → review queue
autonomy.lair_row      = 0.75
dedup.duplicate = 0.92
dedup.update_band = [0.78, 0.92]
max_writes_per_pass = 10
dry_run = true                  # ship safe-by-default
lair_prose_edits = "human-only" # hard rail
```

---

## 11. The loop, end to end (one line per stage)

```
trigger → segment transcript → lair_protocol_check (gate+type+slug+conf)
       → confidence floor (else → review queue)
       → lair_query dedup (duplicate/update/create/pointer/conflict)
       → write-class risk tier (autonomous vs HITL gate)
       → schema-conformant append/prepend/new-file (NEVER overwrite)
       → verify-memory (faithfulness assert)
       → [heartbeat only] consolidate + staleness sweep + graph-integrity
       → lbrain import && embed --stale (mandatory re-sync)
       → pass report to memory/SFMP_LOG/SESSION_<date>.md
```

**Thesis**: SFMP turns the two shipped primitives into a closed control loop by inserting the missing *route → dedup → risk-tier → append-only-write → verify → re-sync* middle, where autonomy scales with `lair_protocol_check`'s confidence crossed with a write-risk tier, every write is append/supersede (never overwrite), source-of-truth `.md` stays authoritative, and the three highest-risk actions — lair creation, lair-prose edits, and supersessions — are always parked in a human-reviewed queue before they become canon.

*Designed by an 8-agent LBrain workflow sweep over a real lair corpus, 2026-05-31.*
