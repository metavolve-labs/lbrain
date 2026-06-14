# LBrain by Metavolve Labs

**AI-native engineering memory with the Lair Protocol.**

LBrain indexes structured markdown lairs and memory files and gives an AI agent fast hybrid (semantic + keyword) search over everything you've chosen to remember — surfacing what's *stored*, never editorializing.

## Why

Existing "RAG" tools index text and forget structure. The lair protocol *is* structure — priority hierarchies, wikilink graphs, frontmatter types, governance cadence. LBrain reads those signals and treats them as first-class retrieval inputs. The protocol is the product; the search engine just respects it.

## What it does

- **Hybrid retrieval** — BM25 (SQLite FTS5) + cosine (sqlite-vec) fused by Reciprocal Rank Fusion, then wikilink graph boost + priority-folder boost + supersession-aware de-ranking + frontmatter-type filter.
- **Call-when-needed precision / recency** — opt-in per query (`rerank=True` for precise lookups; `recency=True` for "latest on X"). Off by default — both are *situational* (measured: rerank helps precise lookups but hurts broad coverage), so they're enabled per call, not globally.
- **Always-on core memory** — an optional curated, user-authored block injected ahead of results (the essentials are always present); gated and token-budgeted by the AMP layer.
- **Prompt-injection containment** — retrieved note text is fenced and framed as untrusted *data*, never instructions, before it reaches the agent.
- **Lair Protocol check** — `should_commit_to_lair(text)` decides what's worth saving so you don't have to think about it.
- **Anti-pattern detection** — cross-checks proposed actions against your saved `feedback_*.md` rules.
- **Onboarding flow** — three-minute questionnaire scaffolds CLAUDE.md + starter priority lairs.
- **MCP server** — direct integration with Claude Code (`claude mcp add -s user lbrain -- /path/to/lbrain-mcp`).

## Stack

- Python 3.10+
- SQLite + sqlite-vec + FTS5 (native, no WASM, no daemon)
- OpenAI text-embedding-3-small (~$0.12 per 6M-token corpus; pennies on updates)
- the official `mcp` SDK (FastMCP) for the MCP server
- ~2,750 LOC core + ~1,370 LOC optional Tier-2 archive subpackage. No moving parts.

## Install

```bash
cd lbrain
pip install -e .            # lean core (index → embed → search → MCP)
# pip install -e ".[rerank]"    # + call-when-needed cross-encoder precision pass
# pip install -e ".[archive]"   # + encrypted Tier-2 archive
# pip install -e ".[arweave]"   # + real permaweb (Arweave L1) writes

# Initialize config + DB
lbrain init --api-key=$OPENAI_API_KEY \
            --source=/path/to/your/lairs \
            --source=/path/to/your/memory

# Walk + ingest
lbrain import

# Embed
lbrain embed --stale
```

## Use

```bash
# Hybrid semantic search
lbrain query "how do we sign C2PA"

# Filter by frontmatter type
lbrain query "code style" --type feedback

# Priority lairs only
lbrain query "current quarter goals" --priority

# Pure keyword (no embedding call, sub-50ms)
lbrain search "snake_case lock"

# "Should I save this?"
lbrain commit-check "user said: don't auto-format imports in this repo"

# "Does this action conflict with anything I've been told?"
lbrain check-action "going to mock the database for these tests"

# Brain stats
lbrain stats
```

## Onboard a new project

```bash
lbrain onboard ~/repos/new-project
```

Three minutes of opinionated questions → working CLAUDE.md + three priority lairs + LAIR_RULES.md.

## Register MCP with Claude Code

```bash
chmod +x /path/to/lbrain/scripts/lbrain-mcp
claude mcp add -s user lbrain -- /path/to/lbrain/scripts/lbrain-mcp
```

Tools surfaced: `lair_query`, `lair_search`, `lair_protocol_check`, `lair_check_action`, `lair_stats`.

## Containerized deployment — for autonomous agents

For agents running in containers / Kubernetes / outside Claude Code, run LBrain as an HTTP MCP service:

```bash
# Local (no container): bind 127.0.0.1 unless you front it with authenticated ingress
# (the server has no built-in auth). Use --host 0.0.0.0 only inside a trusted network.
lbrain mcp --transport streamable-http --host 127.0.0.1 --port 7370

# Docker:
docker build -t lbrain .
# Use a NAMED volume (brain-data) — the container runs as non-root (uid 10001) and a
# host bind mount would inherit host ownership, breaking writes to brain.db. Bind the
# published port to localhost — the MCP server has NO built-in auth, so never publish it
# on a public interface (`-p 7370:7370`); for remote access put an authenticated,
# TLS-terminating reverse proxy in front.
docker run --rm -p 127.0.0.1:7370:7370 -v brain-data:/data \
  -e OPENAI_API_KEY=$OPENAI_API_KEY lbrain

# docker-compose (Kite Apprentice / Maestro pattern):
docker compose -f docker-compose.kite.yml up
```

> ⚠️ The streamable-http MCP server exposes the full tool surface (the whole memory
> corpus is readable) with no authentication. Run it only inside a trusted container
> network or behind authenticated ingress — never directly on the public internet.

The agent's MCP client connects to `http://lbrain:7370/mcp` and gets the same 5 tools. Use this for:

- **Recall for autonomous loops** — agent calls `lair_query` at decision points to pull the relevant stored context (no editorializing — just what's saved).
- **Anti-pattern guarding** — agent calls `lair_check_action` before destructive actions; the saved `feedback_*.md` rules become an automatic safety net.
- **Lair-Protocol output capture** — agent calls `lair_protocol_check` on session outputs to decide what's save-worthy, eliminating the "remind me to remember this" round trip with the user.

### Two-brain pattern (recommended for production agents)

1. **Shared brain** (read-only mount of your full corpus) — global recall + anti-pattern coverage.
2. **Task-specific brain** (per-agent, curated subset) — focused, cheaper, faster. E.g. a Buildathon agent gets only the relevant submission/spec/integration lairs.

Both can run in the same container with different brain.db files via `LBRAIN_HOME` switching, or as separate containers the agent queries in parallel.

See `docker-compose.kite.yml` for a full Apprentice + LBrain wiring example.

## Architecture

```
lbrain/
├── index.py          File walker + frontmatter + chunker + wikilink extractor
├── embed.py          OpenAI embeddings client (batched, stateless)
├── store.py          SQLite + sqlite-vec + FTS5 storage layer
├── search.py         Hybrid BM25 + cosine (RRF) + graph/priority/supersession boosts
├── rerank.py         Optional cross-encoder precision pass (call-when-needed)
├── amp.py            Injection gating, token budgeting, provenance, core memory + fence
├── lair_protocol.py  commit-check heuristic + feedback anti-pattern detector
├── onboard.py        Interactive scaffolding for new projects
├── mcp_server.py     MCP tool surface (FastMCP)
├── cli.py            click CLI entry point
├── config.py         ~/.lbrain/config.toml
└── archive/          OPTIONAL Tier-2 subpackage (install via lbrain[archive])
    ├── archiver.py   encrypt → transport (local/Arweave) → snapshot → index
    ├── crypto.py     AES-256-GCM + Argon2id envelopes + per-item crypto-shred
    ├── storage.py    archive tables + queries (lazy schema, shared connection)
    ├── cli.py        archive/capture/recall/retrieve/shred commands (register hook)
    └── mcp.py        lair_deep_recall tool (register hook)
```

The `archive/` subpackage has a strict one-way dependency on the core (it imports core;
core never imports it except through guarded, lazy registration). `pip install lbrain`
gives the lean retrieval engine; `pip install lbrain[archive]` adds the encrypted Tier-2
archive; `pip install lbrain[arweave]` adds real permaweb writes. Drop the extra (or the
directory) and the core runs unchanged — the archive CLI commands and the
`lair_deep_recall` MCP tool simply don't register.

## Truth hierarchy

Source files (markdown lairs and memory entries) are authoritative. The SQLite index is a derivative cache. If they disagree, trust the file and run `lbrain import && lbrain embed --stale`.

## License

BSD 3-Clause — see [LICENSE](LICENSE). Copyright (c) 2026 Metavolve Labs, Inc.

---

*Metavolve Labs, Inc. — Build the infrastructure of memory for the AI age.*
