# DESIGN — modules

**Status**: format + loader + validator implemented (`lbrain/modules/`) · one bundled module
**Mission**: Give a role a corpus shape on day one without asserting anything about the
organisation that installs it.

---

## What a module is

A **frame, not a payload.** It does not tell you how your job works. It tells your
agent what to ask, what record types the role produces, and how fast each goes
stale. The records it ships are questions. The records that matter are the ones
you write answering them.

This is an inversion of the obvious design, and the reason is mechanical rather
than stylistic.

A declarative module asserts things about *your* organisation that its author
never saw. This engine would then serve those assertions dated, attributed and
`binds` — its own credibility laundering a stranger's guess. That is the exact
failure `docs/DESIGN-evidence-grading.md` exists to catch, arriving through the
distribution channel instead of the corpus.

A question asserts nothing. It cannot be wrong about a company it has never been
inside. And answering it produces a record that is `observed` by the person who
answered — which outranks the question from the moment it exists, on the axis
the question's author does not control.

So the scaffolding retires itself. No graduation mechanism, no deprecation step:
the module goes quiet because the corpus it provoked outgrew it.

---

## Format

```
<name>/
  module.toml          manifest
  README.md            what this is, and what it is not
  questions/*.md       the interrogative records
```

```toml
[module]
name = "role-continuity"          # slug, matches the directory
title = "Role continuity"
version = "0.1.0"
authored = "2026-08-17"           # ISO. a module is copied; a date must ride inside it
description = "..."

[corpus]
doc_types = ["decision", "commitment", "state", "boundary"]
lairs = ["000-PRIORITY-handover", "decisions", "commitments"]

[staleness]
default_days = 90
```

---

## The two invariants

Both are enforced in `modules.validate()`, mechanically, because an advisory rule
is one an exporter forgets.

**1. A module cannot run.** No executable suffixes, no execute bits, no symlinks,
nothing outside `.md`/`.toml`. A module is content a stranger wrote; if it can
also run, "download a module" becomes "run a stranger's code" and the
distribution channel becomes a supply chain.

**2. A module may not ship a record graded `observed`.** Nobody authoring a
module has witnessed anything at the organisation installing it. `sourced` for a
citable claim, `synthesized` for a reasoned one — and every record must carry
both `date:` and `evidence:`, because a module is copied by definition and a
claim date that lives only in an mtime does not survive the copy.

`install()` refuses an invalid module rather than warning, and never overwrites
an existing file — the module exists to produce the user's records, and replacing
one is deleting exactly the thing it was for.

---

## Deferred — formalities to resolve

Recorded as encountered. None of these block the format; all of them block a
commercial handover product.

### The boundary between corporate and personal

A corpus built alongside a role contains at least three separable things, and
only the first is straightforwardly the organisation's: **the work** (decisions,
state, commitments), **the personal** (the holder's own reasoning, drafts,
candid judgement), and **third parties** (colleagues, clients and counterparts
who never agreed to appear in anyone's records).

Current position: the product provides the framework and the enforcement; the
deploying organisation's own people decide where the line falls. That is the
right division — this engine can enforce a boundary that has been drawn and
should not be the party drawing it — but it means the product must make drawing
one *early* the path of least resistance. `questions/004-the-boundary.md` is the
first attempt at that.

Open:
- Sealing on departure — a record that transfers available-for-audit but not
  for-reading. `disclosure.py` classes are the likely mechanism; the lifecycle
  (who seals, when, reversible by whom) is unspecified.
- An IP pass before transfer — who reviews, against what standard, and who
  arbitrates a contested record.
- Whether "mark personal" must be available *before* a departure is announced.
  Almost certainly yes: a boundary drawn while leaving is drawn under pressure
  by a party with an interest in where it lands, and neither side trusts it.

### Regulatory

- **Data protection is now the live surface, not the AI Act.** Capturing a
  person's workflow, communications and contacts makes this a personal-data
  system. Employee monitoring rules, data-subject rights, and the third-party
  mentions above are the questions. The pivot away from distributing algorithmic
  guidance reduced one exposure and created another; do not report it as a net
  reduction without saying so.
- **Scoping rule, adopted:** ship no module covering recruitment, performance
  evaluation, promotion/termination or task allocation. Those are enumerated
  high-risk uses under EU AI Act Annex III point 4. This is a scoping decision,
  cheap to hold and expensive to reverse.
- **Advisory posture:** interrogative content is materially safer than
  declarative here too. Asking what a policy is differs from stating what it
  should be. Keep modules interrogative for this reason as well as the ranking
  one.

### Naming

- `module` is the working noun. `Codex` remains available and is the stronger
  brand; renaming is cheap now and expensive after publication.
- `lbrain setup templates` still means *hook scripts*, which is a misleading name
  now that scaffolding exists under a different word. Renaming it to
  `setup hooks` (aliasing the old spelling) would free the term.

### Not yet built

- Third-party module distribution — signing, a resolvable `gcx://` name per
  module, and verification on install. Blocked on the same identity binding that
  blocks the source axis of the evidence grade.
- Wiring `onboard`'s existing `domain` answer to select a module. The question is
  already asked and its answer currently changes nothing.
- Per-record staleness half-lives. `[staleness] default_days` is parsed and not
  yet consumed by `staleness.py`.

---

*A module that answers is a liability. A module that asks is scaffolding.*
