# LBrain

**Memory for AI agents that cites its sources — and says when it doesn't know.**

Your agent reads a pile of your notes and answers confidently from the wrong one. LBrain serves each
record with its **source, its date, and a flag for whether it actually answers the question asked**.

```bash
pip install "lbrain[local]"
lbrain init --source ~/notes
lbrain import && lbrain embed --stale
lbrain query "what did we decide about the deploy flag"
```

No API key. No account. Nothing leaves your machine.

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
structured — a ~27× spread — while architecture explained ≈**0%** of the variance, with identical
ordering in 8 of 8 models. A smarter model didn't help. Telling the model not to guess didn't help.

**The record handed to the model decides the outcome.** So the fix belongs before generation, on the
input side, and it has to be deterministic — a gate that can't fail the way the generator fails.

## Where it does *not* help

- **Not a hallucination fix.** This is about answering from *retrieved records*. It does nothing
  about a model inventing facts from its weights with no retrieval involved.
- **Small, clean corpora barely benefit.** If your notes are short and unambiguous, ordinary search
  is fine. The gain appears when records are numerous, overlapping, and stale in places.
- **Not a reasoning upgrade.** It changes what the model is given, not what it does with it.
- **The gate is conservative** — it will sometimes flag `near-miss` on a record you'd have accepted.
  That is the trade: fewer confident wrong answers, slightly more "I don't know."

We also killed three of our own claims while building this, including a headline result that turned
out to be an artifact of our own prompt. The retractions are published with the findings.

## How it works

- **Hybrid retrieval** — vector + BM25 keyword, fused by reciprocal rank fusion.
- **Deterministic admissibility gate** — classifies each record against the question as admissible,
  near-miss, or irrelevant. **No model call.**
- **Supersession** — a replaced note stops being *served* but is never deleted. Persistence and
  activation are separate concerns.
- **Honest dating** — each record states whether its date came from the content, the filename, or the
  filesystem. No invented timestamps.
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
| `local` *(default)* | none | nowhere — on-device ONNX, 384-dim |
| `gemini` | your own key | Google, under your key |
| `openai` | your own key | OpenAI, under your key |

`lbrain init` selects `local` when no key is present. See [`docs/KEYS.md`](docs/KEYS.md).

## Something wrong?

```bash
lbrain doctor
```

Prints the **effective** config with per-setting provenance — `[config]` vs `[DEFAULT]` — and whether
your stored vectors match your current embedding settings. Include its output in any issue.

## More

- [`docs/DESIGN-binding-aware-serving.md`](docs/DESIGN-binding-aware-serving.md) — the serving design and its review record
- [`docs/lair-framework/`](docs/lair-framework/) — the organizing convention LBrain reads
- [`contrib/`](contrib/) — session-capture hooks, a shared-key proxy
- [`Dockerfile`](Dockerfile), [`docker-compose.kite.yml`](docker-compose.kite.yml) — containerized deployment

**Truth hierarchy:** your source files are authoritative; the index is a derivative cache. If they
disagree, trust the file and re-run `lbrain import && lbrain embed --stale`.

Requires Python 3.10+. SQLite + sqlite-vec + FTS5 — no daemon, no server, no WASM.

## License

BSD-3-Clause — see [LICENSE](LICENSE). Copyright (c) 2026 Metavolve Labs, Inc.

Patents pending. The licence covers the code; it does not grant patent rights.
