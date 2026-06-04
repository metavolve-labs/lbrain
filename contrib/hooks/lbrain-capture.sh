#!/usr/bin/env bash
# LBrain Tier-2 auto-capture hook (Claude Code SessionEnd / PreCompact).
#
# Reads the hook JSON from stdin, extracts the transcript path + session id, and
# captures the session into LBrain's Tier-2 archive via `lbrain capture` — which is
# idempotent and writes to the OFFLINE LOCAL store by default (no AR spent, no dupes).
#
# Design rules: fail-safe (ALWAYS exit 0 so a capture problem can never break a
# session) and bounded (timeout) so it can't hang the terminal.

set +e
PAYLOAD=$(cat 2>/dev/null || true)

# Extract fields with python3 (no jq dependency); each on its own line so paths with
# spaces survive. Silent no-op if the payload isn't parseable.
INFO=$(printf '%s' "$PAYLOAD" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get("transcript_path", ""))
print(d.get("session_id", ""))
' 2>/dev/null)

TRANSCRIPT=$(printf '%s\n' "$INFO" | sed -n 1p)
SESSION=$(printf '%s\n' "$INFO" | sed -n 2p)

[ -z "$TRANSCRIPT" ] && exit 0
[ -f "$TRANSCRIPT" ] || exit 0

LOG="${LBRAIN_HOME:-$HOME/.lbrain}/capture.log"
LBRAIN_BIN="${LBRAIN_BIN:-lbrain}"   # override if `lbrain` isn't on the hook's PATH

# Bounded + fail-safe. `lbrain capture` exits 3 (and logs) if no passphrase is
# configured, which we swallow — auto-capture is opt-in and silent by design.
timeout 90 "$LBRAIN_BIN" capture \
    --from-file "$TRANSCRIPT" \
    --session-id "$SESSION" \
    --quiet >>"$LOG" 2>&1

exit 0
