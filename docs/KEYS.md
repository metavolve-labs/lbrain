# Embeddings — how your notes get searchable

LBrain turns your notes into vectors so it can search them by meaning. **By default it does
that on your own machine, with no API key and no account.** You only need a key if you
deliberately choose a hosted provider.

If you take nothing else from this page: **you do not need an API key to use LBrain.**

---

## Option A — On-device (the default, no key, no account)

Embeddings are computed locally by a small open model. Your notes are never uploaded.

```bash
pip install "lbrain[local]"
lbrain init --source ./docs --source ./notes
lbrain import && lbrain embed --stale
```

That is the whole setup. No key flag, no signup.

- **Model:** `BAAI/bge-small-en-v1.5` (384-dim), run through `fastembed` / ONNX Runtime.
- **Cost:** zero, forever.
- **Privacy:** your note text does not leave the machine.
- **One honest qualification:** the first run downloads the model itself (~67 MB) from the
  model host. That is a one-time download of *the model*, not an upload of your notes — but
  it is a network call, so we say so rather than claiming a blanket "nothing ever leaves."
  After that, embedding is fully offline.

> **An API key in your environment is never treated as consent.** If you happen to have
> `GEMINI_API_KEY` or `OPENAI_API_KEY` exported, `lbrain init` will *tell you it found one and
> did not use it.* A hosted provider is selected only when you pass the key on the command
> line. (This was a real defect in 0.1.0 — an ambient key silently selected a hosted
> embedder. Fixed in 0.1.1; **0.1.0 is yanked**.)

---

## Option B — Bring your own hosted key

Choose this if you want a hosted provider's embedding quality, or you are already paying for
one. Your text goes to that provider under *your* key and your billing relationship.

```bash
lbrain init --gemini-key <YOUR_KEY> --source ./docs
lbrain import && lbrain embed --stale
```

Get a Gemini key — a free tier is available — at
[Google AI Studio](https://aistudio.google.com/app/apikey). OpenAI works too via `--api-key`.

- **Where the key lives:** `~/.lbrain/env`, `chmod 600`. Never in the plaintext config file.
- **Where your text goes:** to the provider's embedding endpoint, and nowhere else.
- **Cost:** you hold the billing relationship. A typical personal corpus embeds for free or
  for pennies on Gemini's free tier.

**This is less private than Option A**, and we would rather say that plainly than call it
"sovereign." Option A is the private one. Option B is the hosted one.

---

## Option C — A shared proxy someone else operates

Lower friction if you do not want to obtain a key at all. An operator runs a proxy holding the
real key and issues you a revocable token.

```bash
lbrain init --api-base https://the-proxy/v1beta --gemini-key <YOUR_ISSUED_TOKEN>
```

**Read the trade-off before choosing this:**
- ✅ You never hold or risk leaking a real key. The operator can rate-limit and revoke instantly.
- ⚠️ **Your note text transits the operator's server.** This is the *least* private option of
  the four. If your notes are sensitive, use Option A.
- An operator should only ever issue a scoped token. If anyone offers you a raw `AIza…` or
  `sk-…` key to paste, decline — shared raw keys are a security anti-pattern.

Operators: reference proxy in [`contrib/lbrain-proxy/`](../contrib/lbrain-proxy/README.md).

---

## Option D — Your own gateway (teams, corporate egress)

The same `--api-base` mechanism points at *any* Gemini-compatible endpoint: a self-hosted
proxy, a corporate AI gateway, or a VPC egress controller.

```bash
lbrain init --api-base https://your-gateway/v1beta --gemini-key <gateway-token>
```

Lets a team route LBrain through existing key management and audit infrastructure. Combined
with Option A on each workstation, a team can keep note text on-device while still
centralising whatever it does route.

---

## Choosing

| | Notes leave your machine? | Key needed? | Cost |
|---|---|---|---|
| **A — on-device** *(default)* | **no** | **no** | zero |
| **B — your hosted key** | yes, to your provider | yes | your provider's |
| **C — shared proxy** | yes, via the operator | a scoped token | operator's |
| **D — your gateway** | yes, within your infra | gateway token | yours |

**Switching later:** re-run `lbrain init` with new flags at any time, or set `GEMINI_API_KEY` /
`GEMINI_BASE_URL` in `~/.lbrain/env`. Check what is actually live with:

```bash
lbrain doctor
```

`doctor` prints the effective provider and whether each setting came from your config or from a
code default — so you never have to guess which option you are on.

**Changing provider changes the vector space.** If you switch after embedding, re-embed
(`lbrain embed --all`); `lbrain doctor` will flag the drift and exit non-zero until you do.
