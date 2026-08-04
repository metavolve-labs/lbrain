# Contributing to LBrain

Thanks for looking. This document is short on ceremony and specific about what actually helps.

## Before you open an issue

Run this and paste the output:

```bash
lbrain doctor
```

It prints the **effective** configuration with per-setting provenance (`[config]` vs `[DEFAULT]`),
whether your stored vectors match your current embedding settings, and any config keys that are set
but inert. Most reports resolve from that output alone — usually a provider mismatch, or an index
that was never re-embedded after a settings change.

Then check [`docs/DEVELOPER-NOTES.md`](docs/DEVELOPER-NOTES.md) — a running list of symptoms that read as
bugs and are local nuances with their own fix. If yours is there, you're already done; if it's *nearly*
there, say so in the issue, because a near-miss on that list usually means the note needs to be clearer.

Please include: what you expected, what happened, the `doctor` output, and your Python version.

## Development setup

```bash
git clone https://github.com/metavolve-labs/lbrain
cd lbrain
pip install -e ".[local,dev]"
python -m pytest -q          # 79 tests, ~4s, no network required
```

Tests must pass offline. If a change needs a network call to be tested, it needs a fake instead.

## The house rules

These are not style preferences — they're the constraints the project is built on, and a change that
violates one will be sent back regardless of how clean the code is.

**1. The serve path stays deterministic.** `serve.py` and `admissibility.py` must not call a language
model. The entire premise is a gate whose behaviour is inspectable and reproducible. If a feature
seems to need a model call in that path, it belongs somewhere else.

**2. Retrieved text is data, never instructions.** Anything read out of the corpus gets fenced and
labelled before it reaches an agent. Don't add a path that emits unfenced corpus content.

**3. Nothing invented into a record.** Dates say where they came from (`dated` / `file-dated` /
`generated`). If provenance is unknown, say unknown — never synthesize a plausible value.

**4. Measure before you change ranking.** Retrieval changes need a before/after on a real corpus, not
an argument. `bench/ab_search.py` is the harness. "It should be better" is not a result.

**5. Buried isn't deleted.** Supersession changes what gets *served*, never what exists. Don't add a
code path that destroys user records.

**6. Config defaults must match deployed reality.** A default that silently differs from what real
installs run is how you get a system that lies about itself. `lbrain doctor` exists because we
learned this the hard way.

**7. Test the OUTCOME, not the mechanism.** A change to serving, ranking, or chunking is not done
until a test runs the *real* pipeline (`chunk` → `render_response`, or a real index → `search`) and
asserts what a query *returns* — not merely that an internal now carries a value. We learned this the
expensive way: a heading-ancestry fix shipped with a green suite while the served result was
unchanged, because every test asserted "the chunk carries its H1" and none asserted "the query stops
serving the stale figure." *A passing test on a mechanism is not evidence about behaviour.* Outcome
gates live in `tests/test_outcome_serving.py`; a behaviour fix belongs there, and (rule below) it must
fail before your patch **on the outcome**, not on the internal you happened to change.

## Pull requests

- One concern per PR. A ranking change and a CLI change are two PRs.
- Tests for new behaviour. Regression test for a bug fix — the test should fail before your patch.
- Commit messages: say **why**, not just what. The history is documentation here; read a few before
  writing one.
- Run `python -m pytest -q` before pushing.

## Reporting a security issue

Don't open a public issue. Use the private form at **<https://lbrain.ai/security.html>** —
anonymous is fine, and we respond within 5 working days. Email works too:
**curator@golden-codex.com** (a monitored Metavolve Labs mailbox — LBrain is a Metavolve product,
which is why the domain differs). Both routes are delivery-tested, not assumed.

Particularly interested in: anything that gets unfenced corpus text in front of an agent as
instructions, anything that leaks a configured API key, and any path that writes outside the
configured directories.

## Licensing and patents

Contributions are accepted under **BSD-3-Clause**, the project's licence.

LBrain's mechanisms are the subject of pending U.S. patent applications. The BSD-3 licence covers
copyright in the code and **does not grant patent rights** — that's normal for BSD/MIT projects and
is stated plainly so nobody is surprised later. You keep the copyright in your contribution; you're
licensing it under BSD-3 like the rest of the tree.

If you're contributing on behalf of an employer, make sure you're allowed to.
