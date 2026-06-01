# lbrain-proxy — the complimentary-API gateway

A tiny Gemini-compatible forwarding proxy so you can offer LBrain users a
**complimentary API** without ever sharing your real key. The real key lives only
in this server's environment; users get revocable, rate-limited tokens.

## When to use this
- **Lower-friction onboarding** for users who don't want to get their own Gemini key.
- **NOT** for sovereignty-sensitive users (e.g. self-custody-native founders) — their
  embedding text would transit your server. Those users should **bring their own key**
  (see `../../docs/KEYS.md`). Always disclose the trade-off.

## Deploy (Cloud Run)

```bash
# from contrib/lbrain-proxy/
gcloud run deploy lbrain-proxy \
  --source . \
  --region us-west1 \
  --allow-unauthenticated \
  --set-secrets GEMINI_API_KEY=GEMINI_3_API_KEY:latest \
  --set-env-vars LBRAIN_PROXY_TOKENS=tok_sam_a1b2,tok_friend_c3d4 \
  --set-env-vars LBRAIN_PROXY_RATE_PER_MIN=120
```

- `GEMINI_API_KEY` — the REAL key, injected from Secret Manager (never in the image).
- `LBRAIN_PROXY_TOKENS` — comma-separated tokens you issue per user (revoke by removing one + redeploy). Empty = open (don't do that in prod).
- Add a Cloud Run **max-instances** cap + a billing budget alert to bound cost.

## Issue a token to a user
Pick an opaque string (`tok_<who>_<random>`), add it to `LBRAIN_PROXY_TOKENS`, redeploy. Give the user:

```bash
lbrain init --api-base https://lbrain-proxy-XXXX.run.app/v1beta --gemini-key tok_<who>_<random>
```

LBrain then routes all embedding calls through the proxy; the user never sees the real key.

## Cost & abuse controls
- Per-token rate limit (`LBRAIN_PROXY_RATE_PER_MIN`, in-memory; for multi-instance use a shared store).
- Cloud Run max-instances + GCP budget alerts.
- Revoke instantly by dropping a token from the allowlist.

## Local test
```bash
pip install -r requirements.txt
GEMINI_API_KEY=<real> LBRAIN_PROXY_TOKENS=tok_test uvicorn main:app --port 8080
curl localhost:8080/health
```

*Run: 8 lines of trust boundary. The real key never leaves the box.*
