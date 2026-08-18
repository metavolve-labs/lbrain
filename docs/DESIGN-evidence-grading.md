# DESIGN — two-axis evidence grading

**Status**: credibility axis IMPLEMENTED (`lbrain/grading.py`) · source axis blocked on identity binding · **Mission**: Let a record say how well it is known, so a
thing you saw outranks a thing someone reasoned, without either being deleted.

---

## The failure this prevents

A record asserts. A citation makes the assertion look checked. Nothing checked it.

LBrain serves every record dated, attributed and judged — which is the product, and
which is also the exposure. A record reading *"post Tuesdays, 2.3x engagement"* is
served in the same frame as *"we measured 0.917 recall at FAR 0"*: cited, dated,
`binds`. The engine's own credibility does the laundering. This is not hypothetical
for a corpus the owner did not write — an inherited corpus, a shared one, or one
issued to a role.

Dating already solved the *when* half of this. `staleness.claim_date()` refuses to
call an mtime a claim date and names the weak tier `file-dated` so the weakness
survives being read. This document does the *how well known* half, in the same shape.

---

## Why two axes and not one

A single `evidence:` tier collapses two independent facts. A trusted author writing
a synthesized conclusion and an unknown author reporting a direct observation are
not comparable on one scale, and any scale that ranks them has silently decided
which failure it prefers.

The Admiralty System (NATO STANAG 2044 / AJP-2.1), used to process raw intelligence
and adopted since by CTI teams, exists because of exactly this. It grades **source
reliability** (alpha) and **information credibility** (numeric) on two axes that are
scored independently, so that a reliable source cannot validate an uncorroborated
claim by association.

Its documented weakness is operator load: analysts correlate the two axes rather
than holding them apart, and in practice collapse the 6x6 to a 3x3. We adopt the
3x3 collapse from the start.

| Axis | Values | Who sets it |
|---|---|---|
| **Source** — how reliable is whoever authored this | `A` `B` `C` `F` | **Derived. Never authored.** |
| **Credibility** — how well is the claim itself known | `1` `2` `3` `6` | Authored in frontmatter, with a fallback |

The two are displayed and stored as a pair (`A1`, `B3`, `C6`). **They are never
multiplied into one number.** A combined score reintroduces the correlation the
scheme exists to prevent, and there is no arithmetic that makes `B1` and `A3`
comparable — that is the finding, not a gap in it.

---

## Axis 1 — source reliability (derived)

Resolved from the authoring identity of the record, against the reader's own.
Zero author burden: nobody types this, and nobody can inflate it.

| Grade | Meaning | Resolution |
|---|---|---|
| `A` | The reader's own record | authoring identity == this brain's identity |
| `B` | A verified identity inside the reader's org | `gcx://` resolves, attestation verifies, org path is a prefix of the reader's |
| `C` | A verified identity outside the reader's org | signature verifies, no org relationship |
| `F` | Cannot be judged | no identity binding, unsigned, or verification failed |

`F` is the honest default and follows `file-dated`: an ungraded source is *unjudgeable*,
not *fine*. It must never be promoted to `C` because a record looks reasonable.

**Dependency, stated plainly**: this axis is only as real as the record-to-identity
binding. Until authorship is bound and org membership is *verified* rather than
asserted, `B` cannot be distinguished from `C` and both degrade to `F`. That binding
is a prerequisite of this design, not a detail of it.

### The succession property

This axis re-grades an inherited corpus correctly, for free, with no migration.

When a role changes hands, the predecessor's records were `A` to them and become `B`
to the successor — same bytes, same dates, different reader. The successor's own
observations enter at `A1` and begin outranking `B1` on the source axis alone, while
every predecessor record stays permanently retrievable. Handover needs no cleanup
step and no deletion: the corpus re-weights because the reader changed.

---

## Axis 2 — information credibility (authored, with a ladder)

Precedence by strength, strongest first — the `claim_date()` shape.

| Grade | Name | Means | Test |
|---|---|---|---|
| `1` | `observed` | The author directly did, ran, measured or witnessed it | Could the author be a witness to it? |
| `2` | `sourced` | Attributed to a named external artifact a reader can go check | Is there a resolvable citation? |
| `3` | `synthesized` | Reasoned, researched or received. Not witnessed, not one citable source | — |
| `6` | `ungraded` | Nothing declared | fallback |

Set in frontmatter, so it rides inside the file and survives a copy, clone or mtime
reset — the same portability argument that put `date:` in frontmatter:

```yaml
---
date: 2026-08-13
evidence: observed
---
```

`6` is the fallback and is **never silently promoted**. An existing corpus grades `6`
on every record until authored, and that is the correct reading of it: nobody has
said. The label is `ungraded`, not `unknown` and not blank, so it reads as a missing
answer rather than an absent question.

Admiralty's `4` and `5` have no honest author-side use — nobody writes down a claim
they believe is improbable. They are reachable only by **derivation**: a record
contradicted by a later `1` from a source of equal or better reliability degrades to
`5` (*contrary is confirmed*). That is supersession expressed on the credibility axis
rather than a second mechanism.

### Threat model: an author who lies

Nothing at the field level stops `evidence: observed` on a synthesized claim. The
design is built to degrade rather than fail:

- the source axis is **computed**, so an outside author is capped at `C` regardless
  of what they type;
- a false `1` is a signed, attributable claim, which puts reputation behind it;
- the reader's own `A1` outranks a stranger's `C1` on the axis the stranger does not
  control.

A lying author can reach `C1`. They cannot reach `A1`, and `A1` is what the reader
generates by doing the work.

---

## How it reaches the gate — and where it deliberately does not

`admissibility.judge()` answers *does this record answer this question* using
deterministic lexical evidence: specific-term coverage, three-sentence binding
windows, entity anchors. Grading answers a different question — *how well is this
known*. Folding the second into the first would make a single verdict carry two
meanings and make neither debuggable.

**So `judge()` is unchanged.** `ADMISSIBLE | INADMISSIBLE_NEAR | IRRELEVANT` keeps
meaning exactly what it means today. Grading is an orthogonal annotation, carried on
`Hit` beside `mtime` and `heading_path`, and rendered in `serve._header()` next to
the existing date label:

```
[1] E4 registry sweep (2026-08-13)
    src: results-e4.md · chunk 0 · type=project · dated 2026-08-13 · A1 · binds
```

Grading acts in exactly two places, both bounded:

1. **Assembly tie-break** (`search._assemble_topk`) — between hits of comparable
   score, prefer the better-known pair. Never promotes an `IRRELEVANT` hit and never
   reorders across a score gap.
2. **Contradiction** — when two `ADMISSIBLE` hits disagree and one is `1` from a
   source at least as reliable, the observed record leads and the other is marked
   degraded, not dropped. Both stay in the answer. This is the cream-rises mechanic,
   made explicit and bounded rather than emergent.

Everything else — retrieval, fusion, the gate, disclosure — is untouched.

---

## Compatibility

Additive, and off is identical. With no `evidence:` anywhere and no identity binding,
every record grades `F6`, no tie-break fires, no contradiction fires, and output is
byte-for-byte what it is today plus one label. The label is the point: a corpus that
grades `F6` throughout is telling the reader something true about itself.

---

## Open

- ~~**Do `F6` records display the pair, or suppress it?**~~ **Resolved: suppress.**
  The house already decided this twice in the same direction — `type=?` on an
  unrecognised doc type read as an error in the first output a new user saw (A-403),
  and a plain-markdown corpus has no frontmatter at all. A badge on every line of
  every corpus that predates grading is that noise wearing a new name. An ungraded
  record renders exactly as it does today.
- ~~**Where does the ladder live?**~~ **Resolved:** its own module, `lbrain/grading.py`.
  Dating and staleness are one concern and share `staleness.py`; grading is a third.
- **Contradiction detection is unspecified here.** Deciding two records disagree is
  a real problem and is not solved by this document; the rule above is written to be
  inert until it is.

---

*One record's grade is a claim about the record. Two axes keep it from becoming a claim about the author.*

---

## Found while implementing — frontmatter `date:` did not reach the serve path (FIXED)

Not caused by this change, but adjacent to it and it degrades the same header.

`index.parse()` sets `body = post.content`, which is the document with its YAML
block **removed**. `serve.record_date()` resolves the claim date by scanning chunk
text. So the frontmatter `date:` tier in `claim_date()` — the *portable* tier, the
one that exists so a claim date survives a copy, clone or mtime reset — is
unreachable from the serve path on every chunk, not only deep ones as the current
comment in `record_date()` supposes. The value is captured (`Doc.metadata['date']`)
and then never carried to `Chunk` or `Hit`.

Verified: `claim_date(raw_file)` → `('dated', '2026-08-13')`;
`claim_date(parsed_body)` → `('file-dated', <mtime>)`.

It has stayed invisible because the pipeline-level half of the same fix masks it —
a corpus whose filenames carry dates resolves through the `_FN_DATE` tier and looks
correct. A corpus that relies on frontmatter alone — an inherited one, a copied one,
one issued to a role — falls through to `file-dated`. `stale_marker()` is downstream
of the same call, so those records also report `unverified (no claim date)` rather
than an age.

**Fixed.** The claim date is resolved at parse time — the last point at which the
frontmatter still exists — carried on `Doc.claim_date`, stored in a `docs.claim_date`
column, carried on `Hit.doc_date`, and passed into `claim_date()` as a new `fm_date`
argument that sits at the documented `dated` tier. One implementation, unchanged
precedence: `**Last Updated**` still outranks it, and it still outranks a filename
date. Regression coverage in `tests/test_claim_date_reaches_serve.py`, which goes
through the store and the serve path on purpose — a unit test of `claim_date` cannot
catch this class, because `claim_date` was never wrong.

A second gap surfaced on the upgrade path: `doc_metadata_differs()` compared only the
projection columns that existed when it was written, so on an existing brain a
migration-added column would start empty, every unchanged file would be skipped, and
the feature would reach only corpora imported after it shipped. Every
frontmatter-derived column is now in that comparison, so an existing brain heals on
the next plain `lbrain import` — a one-row UPDATE, no re-chunk, no re-embed.
