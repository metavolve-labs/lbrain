<p align="center">
  <a href="https://lbrain.ai"><img src="https://lbrain.ai/assets/lockup.jpg" alt="LBrain — a dragon coiled around a circuit-etched L" width="620"></a>
</p>

<h1 align="center">LBrain</h1>

<p align="center">
  <img src="https://img.shields.io/badge/status-BETA-c9a227?style=flat-square" alt="Status: Beta">
  <img src="https://img.shields.io/badge/license-BSD--3--Clause-555?style=flat-square" alt="BSD-3-Clause">
  <img src="https://img.shields.io/badge/python-3.10%E2%80%933.13-555?style=flat-square" alt="Python 3.10-3.13">
</p>

> **BETA — use at your own risk.** This is early software from a small team. It has not yet been through an
> independent security review. It runs on your machine against your files: **keep backups, and don't point it
> at anything you can't afford to have read by a tool that is still being hardened.** LBrain never deletes
> your source files, but that is a design commitment, not yet an audited guarantee.

**Memory for AI agents that cites its sources — and says when it doesn't know.**

Your agent reads a pile of your notes and answers confidently from the wrong one. LBrain serves each
record with its **source, its date, and a flag for whether it actually answers the question asked**.

```bash
pip install "lbrain[local]"
lbrain init --source ~/notes
lbrain import && lbrain embed --stale
lbrain query "what did we decide about the deploy flag"
```

No API key. No account. Indexing and search run on-device — your documents and queries are never
transmitted.

> ### The one download, and what it's for
>
> On first run LBrain fetches a **~67 MB embedding model** ([`BAAI/bge-small-en-v1.5`](https://huggingface.co/BAAI/bge-small-en-v1.5))
> and then runs it on your CPU. `lbrain init` tells you before it happens and asks; `--yes` skips the prompt.
>
> **That is the model coming *down* — not your documents going *up*.** It is the only network call the
> on-device path ever makes. Afterwards embedding is fully offline, and the model is cached in
> `~/.cache/huggingface` and never fetched again.
>
> **Why a model at all, and not just code?** Searching by meaning needs text turned into coordinates, so
> that *"did anyone test the mailbox"* can find a note that says *"dereference"*, *"round trip"* and
> *"MX record"* — sharing no words with the question. You cannot write rules for that; the number of ways
> to phrase an idea is unbounded. So a model is trained until related meanings land near each other.
> LBrain also runs a plain keyword index (SQLite FTS5) for what you literally typed, and fuses the two.
>
> **What the model is not.** It does not generate text, has no opinions and remembers nothing. It is one
> deterministic forward pass — same text in, same 384 numbers out, every time. 33M parameters whose
> entire behaviour is *text → coordinates*. Closer to a learned lookup table than to a chatbot.
>
> Prefer not to download it? Use a hosted embedder instead, under your own key and billing:
> `lbrain init --gemini-key <KEY> --source ./docs`. See [Embeddings](#embeddings).

```
⟪note⟫
│ Deploy runbook          runbook.md · chunk 0 · dated 2026-05-14 · binds
│ The staging deploy uses tag v2 and the rollback flag is --safe.
⟪/note⟫
```

`binds` means the record answers the question. `near-miss` means it's the right subject but doesn't
contain the answer — the case where retrieval quietly hands over the neighbour's value and the model
presents it as fact.

## Why

We profiled **eight model architectures from seven organizations** on the same near-domain retrieval
task. Failure rates swung **1.3% / 35.7% / 16.7%** depending only on how the *records* were
structured — a **34.4 percentage-point** absolute difference — while architecture explained ≈**0%** of the
variance, with identical
ordering in 8 of 8 models. Changing models didn't remove the effect. Telling the model not to guess
didn't remove it either.

**Across the models we tested, record structure dominated the failure pattern.** So the fix belongs before generation, on the
input side, and it has to be deterministic — a gate judged without another model call.

## Related work — read these too

Four 2026 papers staked adjacent ground while we were building, and they deserve the citation:

- **Deceptive Grounding** — Caruzzo, Yoo & Kim ([arXiv:2607.09349](https://arxiv.org/abs/2607.09349)):
  named and measured the failure mode — RAG responses that pass standard quality checks while attributing
  evidence to the wrong entity, in 8–87% of cases across 13 models, with domain-specialized models
  failing *worse*.
- **MemStrata** ([arXiv:2606.26511](https://arxiv.org/abs/2606.26511)): deterministic supersession rules
  over a bi-temporal ledger, against stale-fact errors on evolving knowledge.
- **Engram** ([arXiv:2606.09900](https://arxiv.org/abs/2606.09900)): a bi-temporal memory graph tracking
  provenance and contradictions — a lean retrieved context beats the full history.
- **Don't Ask the LLM to Track Freshness** ([arXiv:2606.01435](https://arxiv.org/abs/2606.01435)):
  conflict resolution belongs in deterministic code, not model judgment — "the bottleneck … is assembly
  (post-retrieval aggregation), not storage."

We claim no priority over any of this. What our 8-model matrix adds is the controlled variable: it
reproduced the same failure class independently, in a different domain, before it had a name — and showed
the **serving format**, not the model, is the controlling variable. How a record is *presented* to the
generator is the axis these works leave open, and it is the axis LBrain operates on.

## Where it does *not* help

- **Not a hallucination fix.** This is about answering from *retrieved records*. It does nothing
  about a model inventing facts from its weights with no retrieval involved.
- **Small, clean corpora barely benefit.** If your notes are short and unambiguous, ordinary search
  is fine. The gain appears when records are numerous, overlapping, and stale in places.
- **Not a reasoning upgrade.** It changes what the model is given, not what it does with it.
- **The gate is conservative** — it will sometimes flag `near-miss` on a record you'd have accepted.
  That is the trade: fewer confident wrong answers, slightly more "I don't know."
- **A cold import of old notes will serve old decisions as current.** Records are `dated` only when the
  *filename* carries a date, and a bulk copy resets every mtime to today — so newest-wins has nothing to
  order by. Read [this first](docs/DEVELOPER-NOTES.md#10-importing-an-existing-pile-of-notes-yesterdays-doctrine-served-as-todays)
  before importing an archive: either vet it, restore the dates, or knowingly run a calibration period.

We also killed three of our own claims while building this, including a headline result that turned
out to be an artifact of our own prompt. The retractions are published with the findings.

## How it works

- **Hybrid retrieval** — vector + BM25 keyword, fused by reciprocal rank fusion.
- **Deterministic admissibility gate** — classifies each record against the question as admissible,
  near-miss, or irrelevant, without another model call.
- **Supersession** — a replaced note stops being *served* but is never deleted. Persistence and
  activation are separate concerns.
- **Honest dating** — each record states whether its date came from the content, the filename, or the
  filesystem. No invented timestamps. Precedence, strongest first: a `**Last Updated**:` header
  (`verified`) → an `as of <date>` in the body → a YAML frontmatter `date:` **or** a date in the
  filename (`dated`) → mtime (`file-dated`, the weakest — it moves on any edit).
  > **Copying a corpus reages it.** `cp`, a git clone, and most sync tools reset mtime to *now*, so a
  > document whose only date was its mtime serves as `file-dated <the day you copied it>`. Put a
  > `date:` in the file's frontmatter (or the date in its filename) and the claim date rides inside
  > the file — it survives the move. LBrain **indexes `*.md` only**; convert prose you want recalled.
- **Untrusted-data fencing** — retrieved text is fenced and labelled as data, never instructions, so
  a note can't hijack the agent reading it.

## Use it with your agent

Five MCP tools: `lair_query`, `lair_search`, `lair_protocol_check`, `lair_check_action`, `lair_stats`.

```bash
# Claude Code
claude mcp add -s user lbrain -- /path/to/lbrain/scripts/lbrain-mcp

# Any client speaking streamable-http
lbrain mcp --transport streamable-http --host 127.0.0.1 --port 7370
```

> ⚠️ The HTTP server has **no built-in auth** and exposes the whole corpus. Bind to `127.0.0.1`, or
> put authenticated TLS ingress in front. Never publish it on a public interface.

Or skip MCP entirely — `lbrain query`, `search`, `import`, `doctor` all work from the shell.

## Embeddings

| Provider | Setup | Where your text goes |
|---|---|---|
| `local` *(default)* | none | nowhere — on-device ONNX, 384-dim (one-time model download) |
| `gemini` | your own key | Google, under your key |
| `openai` | your own key | OpenAI, under your key |

`lbrain init` uses `local` unless you pass `--gemini-key`/`--api-key` **on the command line**. A key
sitting in your environment is never treated as consent to send your corpus to a third party.
See the [Privacy Policy](https://lbrain.ai/privacy.html) for every network call LBrain can make, and when — §3 lists them all. See [`docs/KEYS.md`](docs/KEYS.md).

## Known issues — read this before filing

Most reports land in one of these. Checking first is faster than waiting for us.

| Symptom | Cause / fix |
|---|---|
| **`TOMLDecodeError` on any command right after `init`, on Windows** | **You are on 0.1.0, which is yanked.** It wrote an unparseable `config.toml` on Windows paths. `pip install -U lbrain` (≥ 0.1.1), then delete `~/.lbrain/config.toml` and re-run `init`. |
| **`UnicodeEncodeError` during `init`, on Windows** | Same fix — 0.1.0 wrote template files in the locale encoding. Fixed in 0.1.1. |
| Build errors installing `[local]` (`fastembed` / `onnxruntime` / `sqlite-vec`) | Native wheels. Upgrade pip first (`pip install -U pip`), which resolves nearly all of these. On Windows a wheel may be missing for a very new Python — 3.10–3.13 are supported; 3.14+ may have no wheel yet. |
| First `embed` pauses, then works | One-time ~67 MB model download. It is the model coming *down*, not your notes going *up*. Offline after that. |
| “It says my provider is Gemini but I never set that” | Run `lbrain doctor`. It marks every setting `[config]` or `[DEFAULT]`. In **0.1.0** an API key in your environment silently selected a hosted provider; 0.1.1 refuses to treat an ambient key as consent. |
| Results changed after switching provider | Changing provider changes the vector space. Re-embed (`lbrain embed --all`); `doctor` exits non-zero on drift until you do. |
| A note is missing from results but the file exists | Usually imported-but-not-embedded. `lbrain stats` shows the gap; `lbrain embed --stale` closes it. |
| I edited only the YAML frontmatter and nothing changed | Known in 0.1.1 and earlier: change detection hashed the body only, so `type:` / `description:` edits were skipped and the old value persisted. **Fixed in the next release** — `import` will report `meta-refreshed: N`. Workaround today: touch the body (a trailing newline is enough) to force a re-index. |
| Two brains on one machine interfering | Use `LBRAIN_HOME=/path/to/brain2` per invocation. |

Not listed? Then it is worth an issue — please use the template, it asks for `lbrain doctor --json`.

## Something wrong?

```bash
lbrain doctor
```

Prints the **effective** config with per-setting provenance — `[config]` vs `[DEFAULT]` — and whether
your stored vectors match your current embedding settings. Include its output in any issue.

[`docs/DEVELOPER-NOTES.md`](docs/DEVELOPER-NOTES.md) covers the symptoms that look like bugs and aren't —
switching embedding providers on an existing brain, two brains on one machine, imported-but-not-embedded
records, and why a note can vanish from results without being deleted.

## More

- [`docs/DESIGN-binding-aware-serving.md`](docs/DESIGN-binding-aware-serving.md) — the serving design and its review record
- [`docs/lair-framework/`](docs/lair-framework/) — the organizing convention LBrain reads
- [`contrib/`](contrib/) — session-capture hooks, a shared-key proxy
- [`Dockerfile`](Dockerfile), [`docker-compose.kite.yml`](docker-compose.kite.yml) — containerized deployment

**Truth hierarchy:** your source files are authoritative; the index is a derivative cache. If they
disagree, trust the file and re-run `lbrain import && lbrain embed --stale`.

Requires Python 3.10+. SQLite + sqlite-vec + FTS5 — no daemon, no server, no WASM.

> **Your Python must be able to load SQLite extensions.** Apple's `/usr/bin/python3` and the
> python.org macOS installers are built without it, and `sqlite-vec` cannot load. Check yours:
> ```bash
> python3 -c "import sqlite3; print(hasattr(sqlite3.connect(':memory:'), 'enable_load_extension'))"
> ```
> If that prints `False`, use Homebrew's (`brew install python@3.12`), or build with
> `PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions"` under pyenv. Linux distro
> packages normally have it enabled.

## License

BSD-3-Clause — see [LICENSE](LICENSE). Copyright (c) 2026 Metavolve Labs, Inc.

Patents pending. The licence covers the code; it does not grant patent rights.
