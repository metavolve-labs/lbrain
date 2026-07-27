# Developer Notes — things that look like bugs but aren't

Every entry here is a symptom that reads as *"LBrain is broken"* and is actually a local nuance with its
own fix. Check here before opening an issue — and **before concluding the code is wrong.**

Run this first. It resolves most of what's below on its own:

```bash
lbrain doctor
```

It prints the **effective** runtime state with per-setting provenance — `[config]` for a value your
`config.toml` actually sets, `[DEFAULT]` for one the code is supplying — plus whether your stored vectors
match your current embedding settings, and any config keys that are set but inert.

> **Why this file exists.** Nearly every entry below has the same shape: *the local environment was
> quietly doing work that the documentation, the defaults, or your memory of the config claimed to do.*
> Nothing is wrong with the code; something is different about the machine. That difference is invisible
> until you print it, which is the entire reason `doctor` exists.

---

## 1. `embed --stale` refuses with "the old vectors live in a different space"

**Symptom**

```
✗ embedding_dim changed since these vectors were built; the old vectors live in a
  different space and mixing them gives meaningless distances.
  Re-embed the whole corpus with `lbrain embed --all`.
```

**What's happening.** You changed `embedding_provider`, `embedding_model`, or `embedding_dim` after
embedding. Vectors from different models are not comparable — a 1536-dim `gemini-embedding-001` vector and
a 384-dim `bge-small-en-v1.5` vector don't merely differ in size, they describe unrelated spaces. Mixing
them produces distances that are arithmetically valid and semantically meaningless, which is *worse* than
an error: search keeps working and silently returns nonsense.

This is the single most likely first bad experience, because the natural first move — switching to the
free local provider on an existing brain — triggers it.

**Fix**

```bash
lbrain embed --all     # drops + rebuilds the vector tables, re-embeds everything
```

`--all` is not "the same thing but slower." It calls `reset_vectors`, which drops and recreates both vec
tables and zeroes every embedded flag, so no stale old-model vector survives in any layer. Tier-2 archives
are invalidated by this and are restored on the next capture.

**Not a bug.** The refusal is the feature. Silently proceeding is the failure mode it was built to prevent.

---

## 2. Two brains on one machine (`LBRAIN_HOME`, and running as a different user)

**Symptom.** Records you know you imported aren't there. Or `doctor` reports settings you don't recognise.
Or the same query answers differently from two terminals.

**What's happening.** Config and DB live at `~/.lbrain/` unless `LBRAIN_HOME` says otherwise. So `root` and
your user account have **entirely separate brains**, as do WSL and Windows, a container and its host, and a
`sudo` invocation versus a plain one. Nothing is lost — you're talking to a different database.

**Fix**

```bash
lbrain doctor | grep db_path      # the only authoritative answer
echo "$LBRAIN_HOME"               # empty means ~/.lbrain
```

**Real example, worth internalising.** On 2026-07-27 a `gh auth refresh` was run twice, reported success
both times, and changed nothing that `git` could see — because the interactive shell and the working
session had different `~/.config/gh/` directories. Two runs were spent debugging OAuth scopes that were
never the problem. **Same tool, same command, two configs.** LBrain has exactly this shape via
`LBRAIN_HOME`; check the path before you debug the behaviour.

---

## 3. A setting in `config.toml` that does nothing

**Symptom.** You set an option, it survives a round-trip, and behaviour doesn't change.

**What's happening.** The key is inert — nothing reads it. Usually a leftover from a renamed or removed
feature. A config file is an *assertion* about behaviour, not proof of it, and an inert key is a
particularly convincing lie because it persists and reloads correctly.

**Fix.** `lbrain doctor` lists keys that are set but unread. Delete them. (Its first run on the authors'
own install found **16.**)

**Related discipline.** `default-value ≠ configured-on ≠ measured-useful`. Three separate claims; verifying
one never establishes another.

---

## 4. Search returns nothing and you're sure the note exists

**Symptom.** `lbrain query` comes back empty on a topic you definitely wrote about.

**What's happening.** Almost always *not indexed yet* rather than *not stored*. Importing and embedding are
separate steps: `import` ingests text, `embed` builds the vectors that semantic search needs. A chunk that
is imported but not embedded is invisible to `query` while being perfectly present in the DB.

**Fix**

```bash
lbrain stats                  # docs / chunks / embedded — if embedded < chunks, that's it
lbrain import <dir> && lbrain embed --stale
```

If `embedded` equals `chunks` and the note still doesn't surface, try `lbrain search "<exact phrase>"` —
FTS5 keyword only, no embeddings. If keyword finds it and semantic doesn't, that's a ranking question, not
a storage one, and *is* worth an issue.

**Note for agent authors:** the `lair_stats` MCP tool exists specifically so an agent can distinguish these
two cases before telling a user their memory is empty. They're different problems with different fixes.

---

## 5. You edited a source file and answers didn't change

**Symptom.** The file on disk says one thing; LBrain keeps serving the old text.

**What's happening.** Working as designed. **Your source files are authoritative; the index is a derivative
cache.** LBrain doesn't watch the filesystem — it reads what was last imported.

**Fix**

```bash
lbrain import <dir> && lbrain embed --stale
```

**If they ever disagree, trust the file.** Never edit the DB to match your memory of a file's contents.

---

## 6. A record "disappeared" — but nothing was deleted

**Symptom.** A note that used to come back stops appearing.

**What's happening.** Supersession. A newer record covering the same ground has taken over, and the old one
stopped being *served*. **Buried isn't deleted.** Persistence and activation are deliberately separate
concerns — the record is still in the database and still in your source file.

**Fix.** None needed, usually. If newest-wins is picking the wrong record, that's a real report — include
both records' dates and `lbrain doctor` output.

---

## 7. A date looks wrong — check the label, not the date

**Symptom.** A record shows a date you didn't write.

**What's happening.** Each date carries its provenance: `dated` (parsed from the content), `file-dated`
(from the filename), `generated` (from the filesystem). A `file-dated` or `generated` record showing an
unexpected date is LBrain being *honest about a weak source*, not inventing a timestamp.

**Fix.** Put a real date in the document. Nothing invents a plausible value to fill the gap — that's a house
rule, not an oversight.

---

## 8. `near-miss` on a record you'd have accepted

**Symptom.** The gate flags a record as `near-miss` when it looks like a fine answer to you.

**What's happening.** The gate is deliberately conservative. `near-miss` means *right subject, doesn't
contain the answer* — the exact case where retrieval hands over a neighbouring entity's value and the model
presents it as fact. Catching that reliably costs some false flags.

**That is the trade:** fewer confident wrong answers, slightly more "I don't know."

**Worth reporting anyway** if you have a case where a record plainly answers the question and is still
flagged. Include the query, the record, and the annotation — false-rejection cases are how the gate's
thresholds get calibrated, and they're published alongside the false-admission rates.

---

## 9. `No module named pytest` on a fresh clone

**Symptom.** You follow CONTRIBUTING, run the suite, and it fails immediately.

**What's happening.** Fixed as of `132e754` — but recorded because of *how* it happened. The setup
instructions installed the package without the test runner, and it went unnoticed for weeks because every
machine that ran it already had `pytest` from something else. **The local environment was doing work the
documentation claimed to do.**

**Fix**

```bash
pip install -e ".[local,dev]"
python -m pytest -q          # 79 tests, ~4s, no network required
```

**The general lesson, which is the point of this file:** "it works on my machine" is not evidence that the
documented path works. A clean venv is cheap and settles it in seconds.

---

## Adding an entry

Add one whenever you catch yourself about to file a bug and discover the cause was local. Keep the shape:
**symptom as the user experiences it → what's actually happening → the fix → whether any part of it *is*
worth reporting.** Quote real error text verbatim; people search for the string they saw.

If the fix required knowing something that isn't discoverable from `lbrain doctor`, that's worth noting
separately — it may mean `doctor` should print one more thing.
