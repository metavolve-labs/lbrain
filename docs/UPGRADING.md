# Upgrading LBrain

The upgrade contract, stated once so nobody has to reverse-engineer it from the code:

**`pip install -U lbrain` is always safe on an existing brain.** Three mechanisms make
that true, and each reports rather than assumes:

1. **Database schema — migrates itself, additively, at open.** Opening a brain created by
   an older version adds any missing columns in place (idempotent `ALTER TABLE`s guarded
   by `PRAGMA table_info`; virtual tables `CREATE IF NOT EXISTS`). Nothing is dropped,
   rewritten, or re-keyed. A brain is never "too old to open."

2. **Chunker changes — detected, self-repaired, never silent.** The index records the
   `CHUNKER_VERSION` that built it. When a new release chunks differently (0.1.4:
   v2 → v3, heading ancestry), retrieval keeps working from the existing chunks and
   `lbrain doctor` reports the drift honestly: *stale, not wrong*. One `lbrain import`
   re-chunks and re-embeds only what changed. Until you run it, you're served yesterday's
   chunk boundaries — a weaker claim, never a wrong one.

3. **Embedding config — drift is a hard stop, on purpose.** If the stored vectors don't
   match the live embedding provider/model/dimensions, `doctor` exits non-zero and says a
   re-embed is required. That one is not self-repaired silently, because it changes what
   retrieval *means*.

**After any upgrade, the whole ritual is:**

```bash
lbrain doctor        # says exactly what, if anything, the new version wants
lbrain import <dirs> # only if doctor flagged chunker drift — repairs it
```

Config keys unknown to an older version are reported by `doctor` as inert (set but
unread), never errors — so a config written by a newer version downgrades losslessly too.

What we promise going forward: schema changes stay additive; anything that cannot be
additive gets an explicit migration command and a CHANGELOG entry that says so in the
first line; `doctor` is always the authority on what an upgrade wants from you.


## After any upgrade: a running server still serves the code it loaded

An MCP server (or any long-lived process that imported `lbrain`) keeps the code it loaded at start.
`pip install -U lbrain`, `git pull` on an editable install, or a hot fix on disk changes **nothing it
serves** until that process is restarted. Measured 2026-09-11: a seat's server served a retired record
unmarked at rank 1 above its correction for hours after the fix had landed on disk.

How to know which code answered: every served response ends with an `[AMP]` footer whose last field is
`engine <version>+<short sha>` (the sha is present on a checkout install, absent on a wheel). Compare it
with `lbrain --version` and `git log -1` on the CLI. **A footer with no `engine` field at all is a
process that predates this field.** The stamp is bound when the code is imported, so it names what the
process loaded, not what is on disk now.

Restart checklist, in order: (0) the client's server entry pins `LBRAIN_HOME` (README, "Connect it to an
agent"): after reconnecting, `lair_whoami` must name the brain you meant, since a server inherits the
client's environment and otherwise opens the default home; (1) the code on disk is what you intend (`lbrain --version`, `git log -1`);
(2) `lbrain query "<anything>"` on the CLI ends with the expected `engine` stamp; (3) restart the MCP
server in every client that holds one (in Claude Code, `/mcp` and reconnect `lbrain`); (4) the same
query through the client's `lair_query` tool shows the same `engine` stamp; (5) for an epoch-managed
home, `lbrain epoch build` if the corpus changed. Keyword `lair_search` has no footer; check with
`lair_query`.
