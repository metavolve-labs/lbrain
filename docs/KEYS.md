# API keys — your two options

LBrain needs an embedding API to turn your notes into searchable vectors. You choose
how that's powered. **The most private option is the default.**

---

## Option A — Bring your own key (recommended, fully sovereign)

Your text only ever goes to Google, under *your* key. Nothing transits anyone else's server. Free for the operator, and the right choice if privacy matters to you.

1. Get a Gemini API key — **free tier available** — at [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Initialize:
   ```bash
   lbrain init --gemini-key <YOUR_KEY> --source ./docs --source ./notes
   lbrain import && lbrain embed --stale
   ```
3. That's it. Your key is written to `~/.lbrain/env` with `chmod 600` — **never** to the plaintext config, never transmitted anywhere except Google's embedding endpoint.

**Cost:** Gemini's `gemini-embedding-001` free tier is generous; a typical personal corpus embeds for free or pennies. You hold the billing relationship.

---

## Option B — Complimentary / shared proxy (lower friction)

If getting your own key is a hassle and you're comfortable with the trade-off, an operator can run a **proxy** that holds the real key and gives you a revocable token. You never handle a real key.

```bash
lbrain init --api-base https://the-proxy/v1beta --gemini-key <YOUR_ISSUED_TOKEN>
```

**Security/privacy trade-off — read this:**
- ✅ You never hold or risk leaking a real key; the operator can rate-limit and revoke your token instantly.
- ⚠️ **Your embedding text transits the operator's proxy server.** It is not end-to-end private the way Option A is. If your notes are sensitive, or sovereignty is the point for you, **use Option A.**
- The operator never shares a raw key — only a scoped token. (If anyone offers you a raw `AIza…`/`sk-…` key to paste, decline: shared raw keys are a security anti-pattern.)

Operators: the reference proxy is in [`contrib/lbrain-proxy/`](../contrib/lbrain-proxy/README.md) — deployable to Cloud Run, real key server-side only.

---

## Option C — Your own proxy / corporate gateway

Same `--api-base` mechanism points at *any* Gemini-compatible endpoint — your own self-hosted proxy, a corporate AI gateway, or a VPC egress controller:

```bash
lbrain init --api-base https://your-gateway/v1beta --gemini-key <gateway-token>
```

Lets a team route LBrain through existing key management / audit infrastructure.

---

**Switching later:** re-run `lbrain init` with new flags any time; or set `GEMINI_API_KEY` / `GEMINI_BASE_URL` in `~/.lbrain/env`. The base URL is the only API setting stored in plaintext config — keys always live in the `600` env file.
