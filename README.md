<p align="center">
  <a href="https://lbrain.ai"><img src="https://lbrain.ai/assets/lockup.jpg" alt="LBrain — a dragon coiled around a circuit-etched L" width="620"></a>
</p>

<h1 align="center">LBrain</h1>

<p align="center">
  <a href="https://pypi.org/project/lbrain/"><img src="https://img.shields.io/pypi/v/lbrain?style=flat-square&label=PyPI" alt="PyPI version"></a>
  <a href="https://pypi.org/project/lbrain/"><img src="https://img.shields.io/pypi/dm/lbrain?style=flat-square&label=installs" alt="PyPI downloads"></a>
  <img src="https://img.shields.io/badge/status-BETA-c9a227?style=flat-square" alt="Status: Beta">
  <img src="https://img.shields.io/badge/license-BSD--3--Clause-555?style=flat-square" alt="BSD-3-Clause">
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.13-555?style=flat-square" alt="Python 3.10-3.13">
</p>

> **Beta.** Early software from a small team, with no independent security audit yet. Keep backups, as
> you would with any beta.

**Local memory for AI that knows which record to trust.**

Your agent can already search your notes. The harder problem starts when your notes disagree.

LBrain retrieves the relevant records, shows where they came from, tells the agent which ones are
current, and flags when a record doesn't actually answer the question.

No API key. No account. Your memory stays on your machine.

<p align="center">
  <a href="https://youtu.be/gYgtFuD9piE"><img src="https://img.youtube.com/vi/gYgtFuD9piE/hqdefault.jpg" alt="Watch: the two-minute XPRIZE submission — Metavolve Labs" width="480"></a>
  <br><em>The two-minute story — our XPRIZE submission.</em>
</p>

## See it work

Say your project contains two notes.

`notes/deploy-flag.md`, from March:

```markdown
Deploys use --safe-mode. Never ship without it.
```

Then, five months later, `notes/deploy-flag-update.md`:

```markdown
--safe-mode is retired. Deploys now use --verify-first.

Supersedes [[deploy-flag]].
```

Now ask:

```
$ lbrain query "what flag do deploys use?"

[1] deploy-flag-update · dated 2026-08-02 · binds
    │ --safe-mode is retired. Deploys now use --verify-first.
[2] postgres-choice · dated 2026-05-20 · near-miss
[3] team-lunch · near-miss
[4] deploy-flag · dated 2026-03-14 · SUPERSEDED · binds
    │ Deploys use --safe-mode. Never ship without it.
```

Real output from the bundled sample corpus, trimmed for width. Reproduce it yourself: the corpus
ships in [`examples/demo-corpus`](examples/demo-corpus), and the terminal-recording script is
[`examples/demo.tape`](examples/demo.tape).

Both deploy notes match the question. Only one is current. LBrain puts the current decision first,
keeps the old one visible but marks it `SUPERSEDED`, and identifies unrelated matches as
`near-miss`. So your agent answers `--verify-first`, with the record that supports it.

## And when nothing answers

Ask the same corpus something it doesn't know:

```
$ lbrain query "what is our production Redis eviction policy?"

[1] redis-evaluation · dated 2026-06-11 · near-miss
    │ Evaluated Redis vs Memcached for the session cache. Redis won on data
    │ structures and persistence options. Eviction policy discussion deferred
    │ until we see production load.
[2] caching-notes · near-miss
...
```

Every match comes back flagged `near-miss`: text about Redis exists, a decision about eviction
policy does not, and nothing pretends otherwise. Finding the nearest text and finding an answer are
not the same operation.

That's deliberate. We'd rather give the model a little more reason to say "I don't know" than
another reason to be confidently wrong.

## What can I give it?

Start with Markdown.

Point LBrain at a directory and it indexes the `.md` files underneath it, subdirectories included.
Your notes don't need a schema:

```markdown
---
date: 2026-08-21
type: decision
---
We picked Postgres over SQLite for the API tier because ...
```

The frontmatter is optional — plain Markdown works. Dates and record types just give LBrain more to
work with.

When one decision replaces another, say so on its own line:

```markdown
Supersedes [[deploy-flag]].
```

LBrain keeps both records. The newer decision takes precedence without erasing the history that came
before it.

**PDFs and Word documents?** Not directly, yet. Convert the ones that matter to Markdown and keep
the originals as your source of truth. The easy way: you already have an agent — tell it *"convert
the PDFs in ~/docs that are relevant to our work into markdown notes."* Agents are good at this, it
takes a minute, and you get to review what your memory is about to be made of. That review is the
point: extraction quality varies enough that we'd rather have you glance at a conversion than
quietly put mangled text into something your AI will later treat as memory.

## Try it

You need **Python 3.10–3.13** whose SQLite can load extensions, and **~67 MB** of disk for the
on-device embedding model. No API key, no account, no cloud service. Check the SQLite requirement
first:

```bash
python3 -c "import sqlite3; print(hasattr(sqlite3.connect(':memory:'), 'enable_load_extension'))"
```

If that prints `False` — Apple's `/usr/bin/python3` and the python.org macOS installers are built
that way — use Homebrew's (`brew install python@3.12`). Linux distro packages normally have it
enabled.

```bash
pip install "lbrain[local]"

lbrain init --source ~/notes
lbrain import
lbrain embed --stale

lbrain query "what did we decide about the deploy flag?"
```

Rather kick the tires before pointing it at your own notes? Both examples above run against the
bundled corpus:

```bash
lbrain init --source examples/demo-corpus
lbrain import && lbrain embed --stale
lbrain query "what flag do deploys use?"
```

Indexing and search run on-device — your documents and queries are never transmitted. The one
network call the local path ever makes is the one-time **~67 MB embedding model download** (that is
the model coming *down*, not your notes going *up*); after that, embedding is fully offline.

> [!IMPORTANT]
> **Importing an old archive?** A cold import of old notes will serve old decisions as current —
> a bulk copy resets every file date to today, so newest-wins has nothing to order by. Read
> [this first](docs/DEVELOPER-NOTES.md#10-importing-an-existing-pile-of-notes-yesterdays-doctrine-served-as-todays):
> either vet it, restore the dates, or knowingly run a calibration period.

## Connect it to an agent

**Start with [`AGENTS.md`](AGENTS.md)** — it is written to the agent itself: how to stand LBrain up
cleanly for its human, and the contract for consuming what it serves.

```bash
# Claude Code
claude mcp add lbrain -- lbrain mcp

# Any client that speaks streamable HTTP
lbrain mcp --transport streamable-http --host 127.0.0.1 --port 7370
```

Five tools over MCP: semantic recall, exact-phrase search, a save-worthiness check, an action check
against your recorded corrections, and corpus statistics. Everything also works from the shell —
`lbrain query`, `search`, `import`, `doctor`.

> ⚠️ The HTTP server has **no built-in auth** and exposes the whole corpus. Bind to `127.0.0.1`, or
> put authenticated TLS ingress in front. Never publish it on a public interface.

## Why use LBrain?

**Because retrieval isn't enough when your history disagrees with itself.** A long-running project
accumulates stale decisions, corrections, near-duplicates, and things that were true six months ago.
Ordinary retrieval finds all of them. LBrain is built to help the agent distinguish among them.

**Because evidence should travel with the answer.** Retrieved records carry their source, date,
status, and whether they actually answer the question — the model has something to cite instead of
just something plausible to repeat.

**Because your memory should belong to you.** Local-first: SQLite, sqlite-vec, FTS5. No account, no
cloud database, no hosted memory service.

Change your model. Change your agent. Change your tools. Keep the history.

## What LBrain doesn't do

LBrain isn't a hallucination cure and it doesn't make a model smarter. It governs the records
retrieved from your corpus; it can't control facts a model invents from its own weights, or
guarantee what the model does with good evidence. And its admissibility check is deliberately
conservative, so it will sometimes flag a record you'd have accepted. That's a trade we're willing
to make: we'd rather surface uncertainty than manufacture confidence.

## How it works

- **Hybrid retrieval** — vector + BM25 keyword, fused by reciprocal rank fusion.
- **Deterministic admissibility check** — classifies each record against the question as admissible,
  near-miss, or irrelevant, without a second model call.
- **Supersession** — a replaced note stops being *served* but is never deleted. Persistence and
  activation are separate concerns.
- **Honest dating** — each record states whether its date came from the content, the filename, or
  the filesystem. No invented timestamps. Precedence, strongest first: a `**Last Updated**:` header
  (`verified`) → an `as of <date>` in the body → frontmatter `date:` or a date in the filename
  (`dated`) → mtime (`file-dated`, the weakest — it moves on any edit).
  > **Copying a corpus reages it.** `cp`, a git clone, and most sync tools reset mtime to *now*. Put
  > a `date:` in the frontmatter and the claim date rides inside the file — it survives the move.
- **Untrusted-data fencing** — retrieved text is fenced and labelled as data, never instructions, so
  a note can't hijack the agent reading it.

## Research behind it

LBrain grew out of a simple question: what happens when the correct answer is present, but the
surrounding records make it easy to use the wrong one?

We profiled eight model architectures from seven organizations on the same near-domain retrieval
task. Failure rates swung 1.3% / 35.7% / 16.7% depending only on how the records were structured —
a 34.4-point absolute difference — while architecture explained roughly none of the variance, with
identical failure ordering in 8 of 8 models. Changing models didn't remove the effect. Telling the
model not to guess didn't either. So the fix belongs before generation, on the input side, and it
has to be deterministic.

The work is published as DOI-backed preprints and datasets, and the experiments are available for
others to challenge: [lbrain.ai/papers](https://lbrain.ai/papers.html). We also killed three of our
own claims while building this; the retractions are published with the findings.

<details>
<summary><b>Related work — read these too</b></summary>

Four 2026 papers staked adjacent ground while we were building, and they deserve the citation:

- **Deceptive Grounding** — Caruzzo, Yoo & Kim ([arXiv:2607.09349](https://arxiv.org/abs/2607.09349)):
  named and measured the failure mode — RAG responses that pass standard quality checks while
  attributing evidence to the wrong entity, in 8–87% of cases across 13 models.
- **MemStrata** ([arXiv:2606.26511](https://arxiv.org/abs/2606.26511)): deterministic supersession
  rules over a bi-temporal ledger, against stale-fact errors on evolving knowledge.
- **Engram** ([arXiv:2606.09900](https://arxiv.org/abs/2606.09900)): a bi-temporal memory graph
  tracking provenance and contradictions — a lean retrieved context beats the full history.
- **Don't Ask the LLM to Track Freshness** ([arXiv:2606.01435](https://arxiv.org/abs/2606.01435)):
  conflict resolution belongs in deterministic code, not model judgment.

What our 8-model matrix adds is the controlled variable: the **serving format**, not the model, is
the controlling variable — the axis these works leave open, and the axis LBrain operates on.

</details>

## Embeddings

| Provider | Setup | Where your text goes |
|---|---|---|
| `local` *(default)* | none | nowhere — on-device ONNX, 384-dim (one-time model download) |
| `gemini` | your own key | Google, under your key |
| `openai` | your own key | OpenAI, under your key |

`lbrain init` uses `local` unless you pass a key **on the command line**. A key sitting in your
environment is never treated as consent to send your corpus to a third party. Every network call
LBrain can make is listed in the [Privacy Policy](https://lbrain.ai/privacy.html), §3. See
[`docs/KEYS.md`](docs/KEYS.md).

## Known issues — read this before filing

Most reports land in one of these. Checking first is faster than waiting for us.

| Symptom | Cause / fix |
|---|---|
| **`TOMLDecodeError` on any command right after `init`, on Windows** | **You are on 0.1.0, which is yanked.** It wrote an unparseable `config.toml` on Windows paths. `pip install -U lbrain` (≥ 0.1.1), then delete `~/.lbrain/config.toml` and re-run `init`. |
| **`UnicodeEncodeError` during `init`, on Windows** | Same fix — 0.1.0 wrote template files in the locale encoding. Fixed in 0.1.1. |
| Build errors installing `[local]` (`fastembed` / `onnxruntime` / `sqlite-vec`) | Native wheels. Upgrade pip first (`pip install -U pip`), which resolves nearly all of these. On Windows a wheel may be missing for a very new Python — 3.10–3.13 are supported; 3.14+ may have no wheel yet. |
| First `embed` pauses, then works | One-time ~67 MB model download. It is the model coming *down*, not your notes going *up*. Offline after that. |
| "It says my provider is Gemini but I never set that" | Run `lbrain doctor`. It marks every setting `[config]` or `[DEFAULT]`. In **0.1.0** an API key in your environment silently selected a hosted provider; 0.1.1 refuses to treat an ambient key as consent. |
| Results changed after switching provider | Changing provider changes the vector space. Re-embed (`lbrain embed --all`); `doctor` exits non-zero on drift until you do. |
| A note is missing from results but the file exists | Usually imported-but-not-embedded. `lbrain stats` shows the gap; `lbrain embed --stale` closes it. |
| I edited only the YAML frontmatter and nothing changed | Known in 0.1.1 and earlier: change detection hashed the body only, so `type:` / `description:` edits were skipped and the old value persisted. **Fixed in the next release** — `import` will report `meta-refreshed: N`. Workaround today: touch the body (a trailing newline is enough) to force a re-index. |
| Two brains on one machine interfering | Use `LBRAIN_HOME=/path/to/brain2` per invocation. |

Not listed? Then it is worth an issue — please use the template, it asks for `lbrain doctor --json`.

## Something wrong?

```bash
lbrain doctor
```

Prints the **effective** config with per-setting provenance — `[config]` vs `[DEFAULT]` — and
whether your stored vectors match your current embedding settings. Include its output in any issue.

[`docs/DEVELOPER-NOTES.md`](docs/DEVELOPER-NOTES.md) covers the symptoms that look like bugs and
aren't — switching embedding providers on an existing brain, two brains on one machine,
imported-but-not-embedded records, and why a note can vanish from results without being deleted.

## Permanent names

A memory that outlives your tools can also have a name that outlives your accounts.
[lbrain.ai](https://lbrain.ai) is the project home; a permanent `gcx://` name of your own can be
claimed at [lbrain.ai/claim.html](https://lbrain.ai/claim.html).

## More

- [`docs/DESIGN-binding-aware-serving.md`](docs/DESIGN-binding-aware-serving.md) — the serving design and its review record
- [`docs/lair-framework/`](docs/lair-framework/) — the organizing convention LBrain reads
- [`contrib/`](contrib/) — session-capture hooks, a shared-key proxy
- [`Dockerfile`](Dockerfile), [`docker-compose.kite.yml`](docker-compose.kite.yml) — containerized deployment

**Truth hierarchy:** your source files are authoritative; the index is a derivative cache. If they
disagree, trust the file and re-run `lbrain import && lbrain embed --stale`.

If LBrain earns a place in your setup, a ⭐ on this repo is the signal that helps the next person
find it.

## License

BSD-3-Clause — see [LICENSE](LICENSE). Copyright (c) 2026 Metavolve Labs, Inc.

Patents pending. The licence covers the code; it does not grant patent rights.
