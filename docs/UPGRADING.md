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
