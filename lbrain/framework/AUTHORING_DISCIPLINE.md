# Authoring Discipline — how to write records that stay true

`LAIR_RULES.md` covers **structure**: line caps, sections, cross-references. This
covers the harder half — **how not to write down something false, and how to make
the falsehood cheap to find later.**

Every rule below came from a specific failure in a working system, not from
principle. They are ordered by how much damage they prevent.

> **What the tool does, and does not, do for you.** LBrain enforces the mechanical
> half: it dates every record honestly, flags staleness at the moment of use,
> buries superseded records instead of deleting them, and fences retrieved text as
> data rather than instructions. **It cannot tell whether what you wrote is true.**
> That is what this document is for. A tool that ranks perfectly over a corpus of
> confident guesses returns confident guesses, faster.

---

## 1. Never collapse the three states

```
default-value   ≠   configured-on   ≠   measured-useful
```

Reading a default in source and recording it as the live setting is the single most
productive source of false records. They are three different claims and they need
three different checks: read the code, read the *running* config, then measure.

**Failure that produced this rule:** four live features were disabled after someone
read code defaults instead of the running configuration. The system broke, and every
document written during the incident was internally consistent and wrong.

**How to apply:** when you write a configuration value into a record, write *how you
know it* — `default` / `configured` / `measured` — not just the number.

---

## 2. `installed` ≠ `applied`

Shipping a change is not the same as the change reaching your data. Derived state —
an index, a cache, an embedding, a generated summary — is produced by code that has
a *version*, and most systems never check it.

**Failure that produced this rule:** a chunking algorithm was improved and shipped.
Re-running the importer reported `unchanged: 729, chunks: 0`, because change
detection hashed document *bodies* and the bodies had not changed. The improvement
was inert on every existing corpus, silently, with no way to notice.

**How to apply:** for any derived artifact, ask *"if the code that produced this
changed, would the stored copy be refreshed?"* If the answer is no, that is a bug
waiting, not a performance optimization.

---

## 3. `configured` ≠ `delivered` ≠ `processed`

An identifier written into a record — an address, a URL, an endpoint, an account —
is an **opaque string** to every layer that handles it. Passing validation, passing
review, and being stored correctly all happen without anything checking that it
*refers to something*.

**Failure that produced this rule:** a plausible-looking alerting address was
introduced as a default. It passed generation, deployment, documentation, live-state
verification and search recall — and silently discarded six months of alerts,
because the domain did not exist. Every layer treated it as a string.

**How to apply:** **dereference before you reference.** Send it, resolve it, hit it,
confirm the far side answered — *then* write it down. And **no acknowledgement means
assume non-delivery, not "pending."** A default that wires an external side effect is
a bug: defaults must be empty and fail loudly, never plausible.

---

## 4. Absence of evidence is not evidence of absence

A search that returns nothing, a count of zero, an empty result — these are facts
about *the system*, and they are constantly recorded as facts about *the world*.

**Failure that produced this rule:** a mistyped storage path caused an empty index to
be created silently. Queries returned "0 results" and statistics reported "0
documents," and a consumer read that as *"nothing is recorded on this subject"* —
a substantive negative claim sourced from an empty database.

**How to apply:** before writing "there is no record of X," confirm you were looking
in a populated place. When you record a negative result, record **where you looked
and how you know it was populated.**

---

## 5. Your own past summary is not evidence

A recalled record is a **point-in-time claim**, not ground truth — including one you
wrote yourself. Confidence in a memory is not correlated with its accuracy, and a
summary that has been re-summarized is further from the source with each pass.

**How to apply:** newest wins, but *verify* rather than assume. When a record names
a file, a setting, or a command, check it still exists before acting on it. Cite the
source, not your memory of the source.

---

## 6. Date every claim by how you know it, not by when you typed it

A record's usefulness decays, and it decays at different rates for different claims.
Anything that drifts with time — prices, balances, rates, counts, versions, "current"
anything — is a **point-in-time claim** and must be re-checked at the moment of use,
never recalled.

**A live sub-figure does not license a stale conversion.** Mixing one fresh number
with one remembered number produces a result that looks freshly computed and is not.

**How to apply:** put an explicit `Last Updated` on the record and, for claims with a
shelf life, a `verify_by` date. Prefer stating the raw observation and its date over
stating a derived conclusion.

---

## 7. Supersede; never delete

When a decision changes, the old record must remain **retrievable but buried**, with
a pointer to what replaced it. Deleting it destroys the ability to ask *"why did we
think that?"* — which is the question that prevents repeating the reasoning.

**Get the direction right.** A supersession edge recorded backwards is worse than
none: it buries the *current* record and serves the *obsolete* one, with every
surface still reporting success.

**How to apply:** `**Supersedes**: [[old-record]]` on the new one. Never edit a
conclusion in place — write a new record and link it.

---

## 8. Fix the class, not the instance

A reported defect is a *sample*. The same mistake is almost always present in places
nobody reported, and fixing only what was reported leaves a system that looks
repaired.

**Failure that produced this rule:** a path-handling bug was reported at two sites.
It existed at four. Writing a general guard rather than patching the two reported
lines is what found the other two.

**How to apply:** when you fix something, state the *predicate* the fix protects,
then search for every place that predicate should hold. Record the count you found,
so the next reader knows the sweep happened.

---

## 9. A claim with no test drifts from the code

Documentation is a claim about behavior. Behavior changes; documentation does not
follow. Within a few months the document is a confident description of something
that no longer happens.

**And a test that cannot fail is worse than no test**, because it manufactures
confidence. The only way to know a test bites is to **break the behavior deliberately
and confirm the test fails.** If it still passes, the test was asserting on something
incidental.

**How to apply:** for every claim you would be embarrassed to have wrong, either pin
it with something that fails when it becomes false, or mark it explicitly as
unverified. "Unverified" is a perfectly good state. "Silently wrong" is not.

---

## 10. Consequence-check before you call it done

*"The tests pass"* is not *"the behavior is preserved."* The tests cover what someone
previously thought to check.

**How to apply:** before recording a change as complete, state what would break if
you were wrong, and check that specific thing. Diff against the prior behavior, not
against your expectation of it. Record the check in the entry — a fix with no
consequence check is a fix nobody can audit.

---

## 11. Reversibility first

When changing the system that holds your knowledge, you are operating on the thing
you would use to recover from a mistake. Back up first. Prefer additive changes.
Know the one-line revert **before** you run the command, not after.

---

## 12. Closing a problem by editing data leaves the mechanism armed

There are two ways to close a defect: change the **code** so it cannot recur, or
change the **data** so the current instance is gone. The second is often correct and
always temporary — the trigger is still there, waiting for the next input that looks
like the old one.

**Failure that produced this rule:** a rendering budget silently truncated an
important document. It was "fixed" by shortening the document. Two months later a
routine edit pushed the document back over the limit and the silent truncation
returned — identical, from the same line of code, which had never been touched.

**How to apply:** when you close an entry, record **which kind of fix it was.** If
you fixed the data, say so and leave the entry open against the code, or write down
the condition that would re-trigger it. "Fixed by editing the file" and "fixed" are
different states and the register should never show them the same way.

---

## 13. Log the anomaly at the moment of discovery

Problems are almost always found at an inconvenient time, in the middle of something
else. The choice is between a five-second note and losing it.

**How to apply:** keep one durable register. Every entry names **who can close it**
and **what the consequence of not closing it is**. Never delete an entry — close it
with what was done. A register you prune is a register that cannot show you your own
patterns, and the patterns are the most valuable thing in it.

---

## The short version

| Never write | Without |
|---|---|
| a config value | how you know it — default / configured / measured |
| an identifier | dereferencing it |
| a negative result | confirming you looked somewhere populated |
| a "current" number | the date you observed it |
| a conclusion | the observation it came from |
| "fixed" | the consequence check |
| "fixed" | saying whether you fixed the CODE or the DATA |
| "done" | the one-line revert |

---

*Part of the LBrain lair framework. These rules cost real incidents to learn; they
are free to adopt.*
