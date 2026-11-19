#!/usr/bin/env bash
# LBrain capture pointer (Claude Code SessionStart, matcher "compact" or "startup").
#
# Prints one line per session captured in the last 24 h: what was captured, whether ordinary recall reaches it yet,
# and five terms to ask `lbrain query` / `lbrain recall` about. All logic lives in `lbrain capture-pointer`; this hook
# only calls it. Thin, bounded, fail-safe: always exits 0, prints nothing when there is nothing to say.
set +e
cat >/dev/null 2>&1   # the hook payload is not needed
LBRAIN_BIN="${LBRAIN_BIN:-lbrain}"
OUT="$(timeout 20 "$LBRAIN_BIN" capture-pointer --hours "${LBRAIN_POINTER_HOURS:-24}" 2>/dev/null)"
if [ -n "$OUT" ]; then
  echo "🧷 CAPTURED SESSIONS (last ${LBRAIN_POINTER_HOURS:-24} h) — the record of what was said before the boundary:"
  printf '%s\n' "$OUT" | sed 's/^/   /'
fi
exit 0
