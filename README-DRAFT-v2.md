<!-- COMPLETE DRAFT — Artiswa voice pass synthesized, all outputs verified against the
     published 0.1.6 wheel on 2026-08-21. Tad: 15-minute read, rough up any sentence
     that doesn't sound like the shop, then this replaces README.md on the readme-v2
     branch. Both demo outputs are REAL. -->

# LBrain

**Local memory for AI that knows which record to trust.**

Your agent can already search your notes. The harder problem starts when your notes
disagree.

LBrain retrieves the relevant records, shows where they came from, tells the agent
which ones are current, and flags when a record doesn't actually answer the question.

No API key. No account. Your memory stays on your machine.

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

Real output from the bundled sample corpus, trimmed for width.

Both deploy notes match the question. Only one is current. LBrain puts the current
decision first, keeps the old one visible but marks it `SUPERSEDED`, and identifies
unrelated matches as `near-miss`. So your agent answers `--verify-first`, with the
record that supports it.

*(GIF: this exact query running in a terminal — examples/demo.tape)*

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

Every match comes back flagged `near-miss`: text about Redis exists, a decision about
eviction policy does not, and nothing pretends otherwise. Finding the nearest text and
finding an answer are not the same operation.

That's deliberate. We'd rather give the model a little more reason to say "I don't
know" than another reason to be confidently wrong.

## What can I give it?

Start with Markdown.

Point LBrain at a directory and it indexes the `.md` files underneath it,
subdirectories included. Your notes don't need a schema:

```markdown
---
date: 2026-08-21
type: decision
---
We picked Postgres over SQLite for the API tier because ...
```

The frontmatter is optional — plain Markdown works. Dates and record types just give
LBrain more to work with.

When one decision replaces another, say so on its own line:

```markdown
Supersedes [[deploy-flag]].
```

LBrain keeps both records. The newer decision takes precedence without erasing the
history that came before it.

**PDFs and Word documents?** Not directly, yet. Convert the ones that matter to
Markdown and keep the originals as your source of truth. The easy way: you already
have an agent — tell it *"convert the PDFs in ~/docs that are relevant to our work
into markdown notes."* Agents are good at this, it takes a minute, and you get to
review what your memory is about to be made of. That review is the point: extraction
quality varies enough that we'd rather have you glance at a conversion than quietly
put mangled text into something your AI will later treat as memory.

## Try it

You need Python 3.10–3.13 with SQLite extension support. The local embedding model
uses about 67 MB of disk. No API key, no account, no cloud service.

```
pip install "lbrain[local]"

lbrain init --source ~/notes
lbrain import
lbrain embed --stale

lbrain query "what did we decide about the deploy flag?"
```

Rather kick the tires before pointing it at your own notes? Both examples above run
against the bundled corpus:

```
lbrain init --source examples/demo-corpus
lbrain import && lbrain embed --stale
lbrain query "what flag do deploys use?"
```

## Connect it to an agent

```
# Claude Code
claude mcp add lbrain -- lbrain mcp

# Any client that speaks streamable HTTP
lbrain mcp --transport streamable-http --host 127.0.0.1 --port 7370
```

Five tools over MCP: semantic recall, exact-phrase search, a save-worthiness check, an
action check against your recorded corrections, and corpus statistics.

## Why use LBrain?

**Because retrieval isn't enough when your history disagrees with itself.** A
long-running project accumulates stale decisions, corrections, near-duplicates, and
things that were true six months ago. Ordinary retrieval finds all of them. LBrain is
built to help the agent distinguish among them.

**Because evidence should travel with the answer.** Retrieved records carry their
source, date, status, and whether they actually answer the question — the model has
something to cite instead of just something plausible to repeat.

**Because your memory should belong to you.** Local-first: SQLite, sqlite-vec, FTS5.
No account, no cloud database, no hosted memory service.

Change your model. Change your agent. Change your tools. Keep the history.

## What LBrain doesn't do

LBrain isn't a hallucination cure and it doesn't make a model smarter. It governs the
records retrieved from your corpus; it can't control facts a model invents from its
own weights, or guarantee what the model does with good evidence. And its
admissibility check is deliberately conservative, so it will sometimes flag a record
you'd have accepted. That's a trade we're willing to make: we'd rather surface
uncertainty than manufacture confidence.

## How it works

- **Hybrid retrieval** — vector + BM25 keyword, fused by reciprocal rank fusion.
- **Deterministic admissibility check** — classifies each record against the question
  as admissible, near-miss, or irrelevant, without a second model call.
- **Supersession** — a replaced note stops being *served* but is never deleted.
  Persistence and activation are separate concerns.
- **Honest dating** — each record states whether its date came from the content, the
  filename, or the filesystem. No invented timestamps. Precedence, strongest first: a
  `**Last Updated**:` header (`verified`) → an `as of <date>` in the body → frontmatter
  `date:` or a date in the filename (`dated`) → mtime (`file-dated`, the weakest — it
  moves on any edit).
  > **Copying a corpus reages it.** `cp`, a git clone, and most sync tools reset mtime
  > to *now*. Put a `date:` in the frontmatter and the claim date rides inside the
  > file — it survives the move.
- **Untrusted-data fencing** — retrieved text is fenced and labelled as data, never
  instructions, so a note can't hijack the agent reading it.

## Research behind it

LBrain grew out of a simple question: what happens when the correct answer is present,
but the surrounding records make it easy to use the wrong one?

We profiled eight model architectures from seven organizations on the same
near-domain retrieval task. Failure rates swung 1.3% / 35.7% / 16.7% depending only on
how the records were structured — a 34.4-point absolute difference — while
architecture explained roughly none of the variance, with identical failure ordering
in 8 of 8 models. Changing models didn't remove the effect. Telling the model not to
guess didn't either. So the fix belongs before generation, on the input side, and it
has to be deterministic.

The work is published as DOI-backed preprints and datasets — not peer-reviewed — and
the experiments are available for others to challenge: [lbrain.ai/papers](https://lbrain.ai/papers.html).

## Embeddings

| Provider | Setup | Where your text goes |
|---|---|---|
| `local` *(default)* | none | nowhere — on-device ONNX, 384-dim (one-time model download) |
| `gemini` | your own key | Google, under your key |
| `openai` | your own key | OpenAI, under your key |

`lbrain init` uses `local` unless you pass a key **on the command line**. A key
sitting in your environment is never treated as consent to send your corpus to a third
party. Every network call LBrain can make is listed in the
[Privacy Policy](https://lbrain.ai/privacy.html), §3. See [`docs/KEYS.md`](docs/KEYS.md).

## Known issues — read this before filing

[KEEP the existing table verbatim — it's already written the right way.]

## Something wrong?

[KEEP existing section.]

## More

[KEEP existing section.]

## License

BSD-3-Clause.
