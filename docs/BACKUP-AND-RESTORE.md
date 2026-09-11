# Backup and restore

This is the procedure a stranger follows. It exists because the 1.0 acceptance row for backup and
restore (A5, CSO run 2026-09-11T05:48Z) failed on exactly one condition: the data survived
everything done to it, and there was no documented route to follow. Four files said "keep backups";
none said how. A recovery that succeeds by a route only the author knows is a claim, not a capability.

Everything below was read from `lbrain/epoch.py`, `lbrain/epoch_build.py` and the live CLI help at
the version this document was written for (0.1.10.post1, lbrain commit after `9f7a4ad`). If the code
and this page disagree, the code is right and this page needs a fix; say so in an issue.

## What a brain is made of

An LBrain home (`~/.lbrain` by default, or `$LBRAIN_HOME`) holds two kinds of thing:

| kind | files | can it be rebuilt? |
|---|---|---|
| **Source of truth** (yours) | the markdown corpus the home points at (`config.toml` → `sources`), `config.toml` itself, `identity.json`, `CORE.md`, `env` | **No.** These are the only things you can lose. They are ordinary files under your control and belong in whatever backup discipline covers your other files. |
| **Derived index** (ours) | `epochs/<id>/brain.db` for each retained epoch, `epochs/CURRENT` (a one-line text file naming the epoch being served), `.failed` and `.building` directories, reader-lease files | **Yes**, from the sources, by `lbrain epoch build`. Losing it costs a rebuild, not data. |

A legacy home (one that has never run `lbrain epoch build`) has a single `brain.db` at the home root
instead of an `epochs/` tree. The same split applies: the db is derived, the sources and config are not.

**The index is a cache of your files.** That is the whole reason restore is cheap. It also means a
backup of `brain.db` alone, without the sources and `config.toml`, is a backup of nothing you cannot
regenerate and everything you can.

## Back up

Copy the home directory as a tree while nothing is building. The pieces that matter, in order:

```bash
H="${LBRAIN_HOME:-$HOME/.lbrain}"
B="$H/backups/home-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$B"
cp -a "$H/config.toml" "$H/identity.json" "$H/CORE.md" "$B/" 2>/dev/null   # whichever exist
[ -f "$H/env" ] && cp -a "$H/env" "$B/"                                     # credentials, if any
cp -a "$H/epochs" "$B/epochs"                                                # the served index and its history
```

Then **prove the copy is readable before you need it**. A backup that has never been opened is a
hope:

```bash
LBRAIN_HOME="$B" lbrain epoch status          # names the epoch CURRENT points at and the retained ones
LBRAIN_HOME="$B" lbrain search "<a phrase you know is in your corpus>"
```

Both commands must answer. If `epoch status` reports that CURRENT names an epoch whose database
does not exist, the copy is torn; take it again while no build is running.

Your **sources** are not in the home. Back them up wherever they live; `config.toml` records the
paths. A restored home pointing at sources that are gone rebuilds an empty index and says so.

**What the acceptance run proved about this copy** (CSO, A5, 2026-09-11): a tree copy of
`config.toml`, `identity.json` and the whole `epochs/` directory served the same records
immediately, with a superseded record still carrying its `SUPERSEDED` marker, and at three
checkpoints (after cloning, after pruning, after a destroy-and-restore) the served text and the
retirement state both matched. Compare **content and retirement state**, not database identity:
identical ids can hide a retired record quietly coming back live.

## Restore

There are two routes, and they are ranked. The documented route is the first one; the second is
what you do when the first is impossible.

### Route 1: put the tree back

Stop anything that writes to the home (an `lbrain epoch build` in progress; nothing else writes).
Then:

```bash
H="${LBRAIN_HOME:-$HOME/.lbrain}"
B=<the backup directory you verified above>
mv "$H/epochs" "$H/epochs.broken-$(date -u +%Y%m%dT%H%M%SZ)"   # keep the wreck; never delete evidence
cp -a "$B/epochs" "$H/epochs"
cp -a "$B/config.toml" "$H/config.toml"                         # and identity.json / CORE.md / env as needed
lbrain epoch status
lbrain search "<the same phrase as before>"
```

The restore is complete when both commands answer as they did from the backup. The served epoch is
whatever `epochs/CURRENT` in the backup named; nothing in the copy needs editing.

### Route 2: rebuild from sources

If there is no usable backup of `epochs/`, or the backup predates source edits you want served:

```bash
lbrain epoch build     # build a candidate from the sources, run the gate, publish atomically
lbrain epoch status
```

This is the only write path into an epoch-managed home. It regenerates the index from the sources
named in `config.toml`. It does not need the old `epochs/` tree at all; if `epochs/CURRENT` is
dangling (see below), remove the pointer deliberately first, then build. What you lose on this route
is **history**: prior epochs, which are earlier states of the index, are not recreated. Nothing about
the corpus itself is lost, because the corpus was never in the home.

### What the system does when CURRENT is dangling

If `epochs/CURRENT` names an epoch whose `brain.db` is missing, every read refuses rather than
silently serving nothing or falling back to an older epoch. The message is:

> CURRENT names epoch '<id>' but <path> does not exist — refusing to fall back silently; restore a
> prior epoch or remove the pointer deliberately

That refusal is correct behaviour and is what the acceptance run saw. "Restore a prior epoch" means
Route 1 (copy the epoch directory back). "Remove the pointer deliberately" means `rm epochs/CURRENT`
followed by Route 2. Do not point CURRENT at another retained epoch by hand: `publish` checks that an
epoch was gate-vetted before it will serve it, and a hand edit skips that check.

## Pruning, and the boundary of what a rebuild can bring back

`lbrain epoch prune --keep N` removes old epochs. Read the sentence exactly:

> `keep` is the number of **prior** epochs retained: not counting CURRENT, not counting any epoch a
> reader currently holds a lease on, and never touching `.failed` forensics.

So `--keep 2` on a home with four epochs leaves **three** directories (CURRENT plus two prior), not
two. The acceptance run measured exactly that and found the CLI help did not say it; the help now
does. `--max-bytes` adds a size cap on top of the count.

**What prune makes unrecoverable:** the pruned epochs themselves, which are earlier states of the
derived index. If a rollback procedure names an epoch by id, check `lbrain epoch status` that the id
is still retained before relying on it; the mail-delist rollback of 2026-09-10 was published
pointing at an epoch that `--keep 3` had already removed, and was recoverable only because Route 2
does not need it. **A rollback that names a pruned epoch is a dead pointer, and the repair is to
rebuild, not to search for the directory.**

**What prune never makes unrecoverable:** the corpus, the config, the identity. Those were never in
`epochs/`.

## Untested, stated rather than implied

The acceptance run exercised a clean tree copy, a prune, and a destroy-and-restore of CURRENT. It did
**not** exercise recovery from a corrupted database, from a partial write, or from a crash in the
middle of the publish swap. `epoch build` writes a candidate, validates it, and repoints CURRENT with
an atomic rename, so a crash mid-build leaves a `.building` or `.failed` directory and an untouched
CURRENT; that design is documented in `ATOMIC-EPOCHS-DESIGN-2026-08-31.md` and has not been
fault-injected as part of this row. Until it is, treat this page as covering the three cases it names.
