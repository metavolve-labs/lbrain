#!/usr/bin/env bash
# check-ip-boundary.sh — the public engine refuses proprietary-mechanism code.
#
# lbrain is BSD-3 PUBLIC: the memory ENGINE (ingest/store/retrieve/serve). The
# erasable-context-window / compaction MECHANISM (KV surgery, pointer-scar,
# relocation-as-eviction, the re-retrieval interval, baton-pass) is PROPRIETARY
# and lives in the private harness (golden_codex_pipeline hooks + lairs), never
# here. The invariant (Tad, 2026-08-26): "the engine gains primitives, never the
# orchestration." This makes that invariant something the repo enforces, not
# something a session has to remember.
#
# Usage:
#   scripts/check-ip-boundary.sh              # scan the whole tracked tree (CI)
#   scripts/check-ip-boundary.sh --staged     # scan staged diff only (pre-commit)
# Escape (reviewed, rare): IP_BOUNDARY_OK="<why this is public-safe>" ... commit
set -euo pipefail
cd "$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"

# SPECIFIC mechanism phrases only — never bare words. "erasable" alone is the
# PUBLIC crypto-shred feature ("permanent but erasable" = destroy the key); the
# proprietary thing is "erasable CONTEXT WINDOW". "compaction" alone can be a DB
# term. Each pattern below names the mechanism unambiguously.
PATTERNS=(
  'kv[- ]?surger'                      # KV surgery
  'pointer[- ]?scar'                   # pointer-scar confabulation control
  'rope[- ]?(phase[- ]?)?repair'       # RoPE phase repair
  'erasable[- ]?context[- ]?window'    # the mechanism, not crypto-shred
  'amendable[- ]?context[- ]?window'
  'baton[- ]?pass'                     # the handoff-record mechanism
  'seam[- ]?(count|first)'             # KV-surgery internals
  'hkvd'                               # HKVD deviation selection
  'relocat[a-z]*[^.]{0,30}(evict|compact|context[- ]?window)'  # relocation-as-eviction
  'N=19/f'                             # the cache-amortized interval trigger
  'price[- ]?ratio[^.]{0,20}trigger'
  'controlled[- ]?compaction'          # the productized feature name
)

MODE="${1:-full}"
if [ "$MODE" = "--staged" ]; then
  SUBJECT="$(git diff --cached -U0 -- ':!scripts/check-ip-boundary.sh' ':!*.md' 2>/dev/null | grep '^+' || true)"
else
  # tracked source only; docs (.md) describe freely — the papers are public.
  FILES="$(git ls-files -- ':!*.md' ':!scripts/check-ip-boundary.sh' ':!LICENSE')"
  SUBJECT="$(printf '%s\n' "$FILES" | tr '\n' '\0' | xargs -0 grep -In '' 2>/dev/null || true)"
fi

HITS=""
for pat in "${PATTERNS[@]}"; do
  found="$(printf '%s\n' "$SUBJECT" | grep -iEn "$pat" || true)"
  [ -n "$found" ] && HITS+="  [pattern: $pat]\n$found\n"
done

if [ -n "$HITS" ]; then
  if [ -n "${IP_BOUNDARY_OK:-}" ]; then
    echo "⚠ IP-boundary: proprietary-mechanism vocabulary present, WAIVED:" >&2
    echo "  reason: $IP_BOUNDARY_OK" >&2
    exit 0
  fi
  echo "⛔ IP BOUNDARY — this is the PUBLIC engine; proprietary mechanism vocabulary found:" >&2
  echo -e "$HITS" >&2
  echo "" >&2
  echo "  lbrain (BSD-3) is the memory ENGINE. The erasable-context-window /" >&2
  echo "  compaction MECHANISM stays in the private harness — the engine gains" >&2
  echo "  primitives, never the orchestration (Tad, 2026-08-26)." >&2
  echo "  If this genuinely describes a PUBLIC primitive, re-run with" >&2
  echo "  IP_BOUNDARY_OK=\"<why it is public-safe>\" and it will be recorded." >&2
  exit 1
fi
echo "✓ IP boundary clean — no proprietary-mechanism vocabulary in tracked source."
