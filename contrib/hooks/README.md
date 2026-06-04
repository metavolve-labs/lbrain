# LBrain Tier-2 auto-capture hook (opt-in)

Turn every Claude Code session into a permanent, recall-able Tier-2 record automatically —
no manual `lbrain archive`. This is the **Capture layer** of the stack: comprehensive
episodic memory at the substrate, curated retrieval at the surface.

## What it does

On `SessionEnd` (and optionally `PreCompact`), the hook hands the session transcript to
`lbrain capture`, which:

- **Writes to the OFFLINE local store by default** — free, no AR spent. (Push to Arweave
  is a separate, deliberate step; see "Going permanent" below.)
- **Is idempotent** — dedups on a stable hash of the transcript content, so re-firing on
  the same session is a no-op. No duplicates, no wasted work.
- **Is non-interactive + fail-safe** — never prompts; if no passphrase is configured it
  skips silently; any error still exits 0, so it can never break your session.
- **Indexes a fast offline snapshot** — extractive (no LLM call), so session-end stays
  instant. The full record is stored intact, so a richer snapshot can be re-derived later.

After it runs, `lbrain recall "<query>"` (or the `lair_deep_recall` MCP tool) finds the session.

## Prerequisites

1. LBrain installed and initialized (`lbrain init …`), with an embedding key for semantic recall.
2. An **archive passphrase** configured (the encryption is real even for local capture):
   - local: `LBRAIN_ARCHIVE_PASSPHRASE=…` in `~/.lbrain/env`, **or**
   - a secret reference: `LBRAIN_ARCHIVE_PASSPHRASE=gcp-secret:<project>/<secret>`.
   Without it, capture skips (exit 3) — by design.
3. `lbrain` on the hook's `PATH` (or set `LBRAIN_BIN=/abs/path/to/lbrain` in the hook env).

## Install

```bash
chmod +x contrib/hooks/lbrain-capture.sh
# Merge contrib/hooks/hooks.snippet.json into ~/.claude/settings.json under "hooks",
# replacing ABSOLUTE_PATH with this repo's absolute path. Restart Claude Code.
```

Verify: end a session, then `tail ~/.lbrain/capture.log` and `lbrain archives`.

## Going permanent (Arweave)

Local capture is the **staging tier**. To push a captured record to the permaweb:

```bash
lbrain capture --from-file <transcript> --remote     # one record, deliberate, spends AR
```

Comprehensive free permanence (every session → Arweave at no cost) lands with the
**free `up.arweave.net` bundler** (WS-5, <100 KiB ANS-104 free). Until then, keep
auto-capture local and push deliberately, to protect the AR balance.

## Safety notes

- **Opt-in only.** Nothing auto-runs until you install the hook into your own settings.
- **Cost:** zero by default (local transport). `--remote` is the only path that spends AR.
- **Privacy:** records are AES-256-GCM encrypted; the key is local and crypto-shreddable
  (`lbrain shred --txid …`). Auto-capturing *everything* makes throwaway sessions permanent
  too — use `lbrain shred` to drop ones you don't want kept.
