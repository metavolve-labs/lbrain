# Embeddings — your four options

LBrain turns your notes into searchable vectors. **By default it does that on your own
machine, with no key and no account** — nothing is transmitted. Everything below is opt-in,
in descending order of privacy.

| Option | Setup | Where your text goes |
|---|---|---|
| **A — On-device** *(default)* | none | nowhere |
| B — Your own key | Gemini or OpenAI key | that provider, under your key |
| C — Shared proxy | issued token | the operator's server, then the provider |
| D — Your own gateway | your endpoint | wherever you route it |

---

## Option A — On-device (default, no key, fully private)

```bash
lbrain init --source ./docs --source ./notes
lbrain import && lbrain embed --stale
```

That's the whole setup. Embeddings are computed locally with ONNX (`bge-small-en-v1.5`,
384-dim). Your documents and your queries never leave the machine.

LBrain downloads the ~67 MB embedding model once on first run — a one-time artifact fetch
that carries none of your content — and works offline afterwards.

**A key in your environment is never treated as consent.** If `GEMINI_API_KEY` or
`OPENAI_API_KEY` happens to be set, `lbrain init` ignores it, stays on-device, and says so.
A hosted provider is used only when you ask for one on the command line.

---

## Option B — Bring your own key (sovereign, hosted quality)

Your text goes only to Google or OpenAI, under *your* key, on your billing relationship.
Nothing transits anyone else's server.

```bash
lbrain init --provider gemini --gemini-key <YOUR_KEY> --source ./docs
lbrain import && lbrain embed --stale
```

1. Gemini keys — **free tier available** — at [Google AI Studio](https://aistudio.google.com/app/apikey).
   OpenAI: use `--provider openai --api-key <YOUR_KEY>`.
2. Your key is written to `~/.lbrain/env` with `chmod 600` (parent dir `0700`), written
   atomically — **never** to the plaintext config, and never transmitted anywhere except the
   provider's embedding endpoint.

**Cost:** `gemini-embedding-001`'s free tier is generous; a typical personal corpus embeds
for free or pennies.

> **Switching an existing brain — `--provider` is required.** On a brain that already has a
> provider, `lbrain init` keeps it, deliberately, so an install is never switched out from
> under its owner. `lbrain init --gemini-key <KEY>` alone will **not** switch you and will
> not store the key. Pass `--provider gemini` (or `openai`) explicitly.
>
> Note also that the configured `embedding_dim` carries over — switch a 384-dim on-device
> brain to Gemini and you get 384-dim Gemini vectors, not the model's default 1536. To use
> the full width, set `embedding_dim = 1536` in `~/.lbrain/config.toml` and re-embed. Either
> way, run `lbrain embed --stale` after switching: old vectors live in a different semantic
> space, and `lbrain doctor` will report the mismatch.

---

## Option C — Complimentary / shared proxy (lower friction)

If getting your own key is a hassle and you accept the trade-off, an operator can run a
**proxy** that holds the real key and issues you a revocable token. You never handle a real key.

```bash
lbrain init --provider gemini --api-base https://the-proxy/v1beta --gemini-key <YOUR_ISSUED_TOKEN>
```

**Read the trade-off:**
- ✅ You never hold or risk leaking a real key; the operator can rate-limit and revoke instantly.
- ⚠️ **Your embedding text transits the operator's server.** It is not private the way A or B
  are. If your notes are sensitive, or sovereignty is the point, use **A** or **B**.
- The operator issues a scoped token, never a raw key. If anyone offers you a raw `AIza…` /
  `sk-…` key to paste, decline — shared raw keys are a security anti-pattern.

Operators: the reference proxy is in [`contrib/lbrain-proxy/`](../contrib/lbrain-proxy/README.md)
— deployable to Cloud Run, real key server-side only.

---

## Option D — Your own proxy / corporate gateway

The same `--api-base` mechanism points at *any* Gemini-compatible endpoint — a self-hosted
proxy, a corporate AI gateway, or a VPC egress controller:

```bash
lbrain init --provider gemini --api-base https://your-gateway/v1beta --gemini-key <gateway-token>
```

Lets a team route LBrain through existing key-management and audit infrastructure.

---

**Changing settings later:** re-run `lbrain init` with the flags you want — remembering that
the provider itself only changes when you pass `--provider`. You can also set
`GEMINI_API_KEY` / `GEMINI_BASE_URL` directly in `~/.lbrain/env`. The base URL is the only
API setting kept in plaintext config; keys always live in the `600` env file.

**Not sure what's actually in effect?** `lbrain doctor` prints the effective configuration
with per-setting provenance — `[config]` vs `[DEFAULT]` — and whether your stored vectors
match your current embedding settings.
