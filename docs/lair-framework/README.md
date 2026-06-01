# The Lair Framework

A portable knowledge-base discipline for builders working with AI agents. A **lair** is a single, AI-optimized markdown document that lets any agent recover full context on a system *cold* — front-loaded status, tables over prose, one concern per file, a navigable cross-reference graph. LBrain indexes lairs (+ a rolling memory journal) for hybrid semantic + keyword recall.

This directory is the genericized, domain-agnostic framework — distilled from a real, battle-tested lair corpus by an 8-agent LBrain workflow sweep (2026-05-31).

## The four pieces

| File | What it is |
|------|-----------|
| [`LAIR_TEMPLATE.md`](LAIR_TEMPLATE.md) | The copy-me template for a single lair. Front-loads Status/Priority/Mission; everything else is tables. |
| [`LAIR_RULES.md`](LAIR_RULES.md) | The authoring contract: 500-line cap, naming, required sections, mandatory bidirectional cross-refs, staleness policy, re-sync discipline. |
| [`FAST_START_PROTOCOL.md`](FAST_START_PROTOCOL.md) | `lbrain lair from-repo <path>` — auto-convert a repo + its README/CLAUDE.md into a filled, lint-passing lair. Deterministic harvest + LLM fill + validator. |
| [`SELF_FILLING_PROTOCOL.md`](SELF_FILLING_PROTOCOL.md) | **SFMP** — the autonomous-memory loop. Periodically decides what to remember and append/update/create lairs+memories on its own, built on LBrain's shipped `lair_protocol_check` + `consolidate` primitives. Append-only, never overwrites, high-risk writes are human-gated. |

## The arc

1. **Start fast** — point the fast-start protocol at your repos; get a filled lair per project in minutes.
2. **Stay current by hand** — follow `LAIR_RULES.md`; re-sync the index after edits.
3. **Then let it self-fill** — turn on SFMP so the system captures decisions, dedups against what exists, and grows its own memory while you build.

## Why it works

It's engineered for an **AI reading cold**, with the human as secondary reader. Front-loaded status answers "what is this / is it alive / what's the one next thing" before any scrolling. Tables embed and chunk cleanly. The mandatory cross-reference graph keeps the corpus navigable and self-diagnosing. And because source `.md` files are always authoritative (the vector index is a derivative cache), nothing the automation does can corrupt your ground truth — every write is append-or-supersede, never overwrite.

---

*Part of [LBrain](../../README.md). Metavolve Labs, Inc. — BSD-3-Clause.*
