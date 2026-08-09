# Design — manifest-scoped views (the copy → view migration)

**Status:** DESIGN ONLY, no code. Authored by the CSO 2026-08-06 under the CTO's green light
(*"Design first, no code — your own terms"*).
**Closes:** ORG-ARCHITECTURE §5's named gap · A-450 (persona isolation) by construction.

---

## 1. The problem, with today's evidence

`ORG-ARCHITECTURE` §5 states the target: *an agent is a **scoped VIEW** over the shared lairs — a
MANIFEST that includes only its lane, read live from the one source.* And it states the gap:

> *"LBrain today indexes a **directory**, which is why the CSO copies… The copy is the MVP; the view
> is the architecture; the view is the feature to build."*

Scoping is currently achieved by **physically copying a subset** (`scripts/sync-corpus.sh`, driven by
`personas/<role>/MANIFEST.txt`). The filter *is* the copy. That works at N=1 and degrades at N>1:
N copies, N drifts, N secret-gate passes, and claim dates reaged on every copy.

**It has already failed once, observably.** On 2026-08-06 the CTO applied three corpus fixes
(S-019, S-025) to the pipeline **source** and asked the CSO to re-sync and close them. The CSO
could not: `sync-corpus.sh` roots at an absolute path on the pipeline host, which does not
exist on the CSO's machine. The evidence copies remained byte-identical to their old seals. **A
correction was made at the source and was invisible to the only reviewer who was asked to confirm
it.** That is the copy model's failure mode, not an operator error.

## 2. Two places the filter could live

### Option A — index-time (RECOMMENDED)
`discover()` filters against the MANIFEST. Excluded files **never enter the agent's index.**
Per-agent DB, built from shared sources.

### Option B — serve-time
One shared index; a per-agent SEE-filter applied on read.

## 3. Why A, on this codebase's own record

Option B is more elegant on paper and it is the wrong choice here, because **this repository has
already shipped a serve-time scope filter that leaked, and a serve-time annotation that missed a
path.**

- **The MCP↔disclosure seam.** `allowed_path_prefixes` / `allowed_doc_types` / `force_priority_only`
  already exist — in `disclosure.py`, applied at serve time. `check_action` **bypassed that scope
  entirely** and was fixed as CRITICAL (`a0fa009`). A scope filter that had to be applied on every
  read path was not applied on one.
- **A-410.** The `SUPERSEDED` badge was derived from `boosts`, which only the *ranked* search path
  populated — so the product's flagship differentiator was invisible on the keyword path. Same
  shape: a per-path obligation, honoured on one path.

The read surfaces that would each need the filter: vector search, FTS, `amp`, `serve`,
`check_action`, MCP resources, `consolidate`, `recall`, beliefs. **Nine chances to miss one**, on a
property where missing one is a disclosure breach, not a degraded result.

Option A has **one** enforcement point and its failure mode is a file that is absent rather than a
file that leaked. Isolation becomes structural: A-450 is closed because the record is not there, not
because nine call sites remembered to check. This is req 13 — *a load-bearing binding must be ACTIVE,
not passive* — applied to scoping.

**Keep the serve-time knobs.** They are disclosure/blinding (what a *caller* may see of what the
brain holds), a different axis from scope (what the *brain* holds at all). Defence in depth; not a
substitute.

## 4. The MANIFEST as the unit

Already the source of truth (`sync-corpus.sh`: *"The manifest is the source of truth, not the copied
files"*). Proposed contract:

- **Location:** `personas/<role>/MANIFEST.txt`, one path or glob per line, `#` comments,
  `!` prefix to exclude. Resolved relative to a **lairs root**, supplied once (config `lairs_root`),
  never embedded per-entry.
- **Deny wins over allow**, unconditionally. An exclusion must not be defeatable by ordering.
- **An entry matching nothing is an ERROR, not a silent no-op** — A-438's class (the
  mistake-prevention tool that was inert). A manifest that has drifted from the corpus must say so
  at index time, loudly, rather than quietly narrowing the agent's world.
- **The MANIFEST is the audit artifact.** `git diff MANIFEST.txt` is the complete, reviewable answer
  to *"what changed about this persona's view?"* — which is the property that makes scoping by
  MANIFEST better than scoping by prompt.

## 5. Where the secret gate moves

Today `scan-secrets.sh` gates a **push** of a copy. With no copy there is no push — but the agent can
still *see* files, so the gate must move **earlier**, to manifest evaluation: a MANIFEST whose
resolved file set includes forbidden content **fails at index time**. Same rules, enforced where the
inclusion decision is actually made.

(Note the gate itself was silently inert on macOS until 2026-08-06 — **A-532**. Moving it onto the
index path makes it load-bearing, so its own liveness needs a test, not an assumption.)

## 6. How we prove isolation holds

Outcome tests, per CONTRIBUTING rule 7 — *test the OUTCOME, not the mechanism*:

1. **Two personas, one source, disjoint manifests.** Build both. Assert persona A cannot retrieve
   persona B's content **through every read surface**, enumerated explicitly. Not "the filter was
   called" — *"the content is unreachable."*
2. **Mutation test the enforcement point.** Break the filter; both suites must fail. A test that
   passes on a broken filter is the `doctor` false-all-clear again.
3. **Empty-match test.** A manifest entry matching nothing fails the build.
4. **Deny-precedence test.** An excluded file stays excluded regardless of rule order.

## 7. What this does NOT solve — stated plainly

**Manifest views do not fix the cross-machine problem.** A view over sources a machine cannot reach
is still nothing. Today's S-019/S-025 failure is a *distribution* failure, and this design does not
address it — that needs the `lbrain wear` / registrar / Arweave layer (`ORG-ARCHITECTURE` §8).

Conflating the two would be the expensive mistake here. **Views fix scoping and drift on a machine
that can reach the sources. Distribution is a separate build.** What views *do* contribute to
portability is making the transportable unit small — a `CORE.md` plus a MANIFEST is kilobytes, where
a corpus copy is not.

## 8. Migration

Additive. `sources` keeps working; a persona opts in by declaring `lairs_root` + `manifest`. The CSO
migrates last, not first — it is mid-lane and is the regression canary. Copy and view can be diffed
against each other during transition, which is a free correctness check nobody has to write.

## 9. Open questions for the CTO

1. **Per-agent DB, or one DB with an agent column?** This design assumes per-agent (isolation is the
   filesystem's job). One DB is cheaper and reintroduces exactly the per-query filter Option B was
   rejected for.
2. **Does the MANIFEST pin a corpus version?** The owner answered *pinned* for `wear`. If views pin too,
   pinning belongs in the MANIFEST and reproducibility follows for free.
3. **Glob dialect** — full globs, or literal paths plus directory prefixes? Narrower is easier to
   audit, and the MANIFEST's value is that a human can read it.
4. **Does an agent ever see its own MANIFEST?** Arguably yes — knowing the shape of your own blind
   spot is a scientific virtue. It is also a disclosure decision, so it is not mine alone.
