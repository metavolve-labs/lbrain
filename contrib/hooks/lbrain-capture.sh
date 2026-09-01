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
print(d.get("transcript_path") or d.get("transcriptPath") or "")
print(d.get("session_id") or d.get("sessionId") or "")
' 2>/dev/null)

TRANSCRIPT=$(printf '%s\n' "$INFO" | sed -n 1p)
SESSION=$(printf '%s\n' "$INFO" | sed -n 2p)
[ -z "$SESSION" ] && SESSION="${GROK_SESSION_ID:-}"

# Grok's payload NAMES a transcript_path, but it points at the RPC event log — an
# existing file with zero extractable turns (A-546 producer class, measured
# 2026-09-01: updates.jsonl 4181 rows / 0 turns; chat_history.jsonl 106 / 76).
# Presence of the file is not fitness of the file: reject RPC names outright, then
# fall back to chat_history.jsonl beside it (or by session-id search below).
case "$(basename -- "${TRANSCRIPT:-}")" in
  updates.jsonl|events.jsonl|signals.json|rewind_points.jsonl)
    SIB="$(dirname -- "$TRANSCRIPT")/chat_history.jsonl"
    if [ -f "$SIB" ]; then TRANSCRIPT="$SIB"; else TRANSCRIPT=""; fi
    ;;
esac

# Grok may also omit transcript_path entirely; chat_history.jsonl is the pre-boundary record.
if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  if [ -n "$SESSION" ]; then
    TRANSCRIPT="$(python3 -c '
import sys
from pathlib import Path
sid = sys.argv[1]
root = Path.home() / ".grok" / "sessions"
if not sid or not root.is_dir():
    sys.exit(0)
for p in root.rglob("chat_history.jsonl"):
    if sid in str(p.parent):
        print(p)
        break
' "$SESSION" 2>/dev/null)"
  fi
fi

[ -z "$TRANSCRIPT" ] && exit 0
[ -f "$TRANSCRIPT" ] || exit 0

# Under a Grok harness the hook env may not carry LBRAIN_HOME; deployments set
# GROK_LBRAIN_HOME in the launcher (never hardcode a box-specific path here).
# A seat-harness hook with NO explicit home must REFUSE, not fall back to the
# org brain: capture landing in ~/.lbrain looks like memory working while the
# seat brain starves — presence of a rich brain is not fitness of this brain.
# Refusal is logged OUTSIDE any brain home and exits 0 (never break a compact).
if [ -n "${GROK_HOOK_EVENT:-}" ] || [ -n "${GROK_SESSION_ID:-}" ]; then
  if [ -z "${LBRAIN_HOME:-}" ] && [ -z "${GROK_LBRAIN_HOME:-}" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) REFUSED capture: seat-harness hook with no LBRAIN_HOME/GROK_LBRAIN_HOME — org-brain fallback disabled; session=${SESSION:-unknown} transcript=$TRANSCRIPT" \
      >>"$HOME/.lbrain-capture-refusals.log" 2>/dev/null
    exit 0
  fi
  export LBRAIN_HOME="${LBRAIN_HOME:-$GROK_LBRAIN_HOME}"
fi
LOG="${LBRAIN_HOME:-$HOME/.lbrain}/capture.log"
LBRAIN_BIN="${LBRAIN_BIN:-lbrain}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

if "$LBRAIN_BIN" capture --help >/dev/null 2>&1; then
  timeout 90 "$LBRAIN_BIN" capture \
      --from-file "$TRANSCRIPT" \
      --session-id "$SESSION" \
      --quiet >>"$LOG" 2>&1
else
  # Public CLI has no `capture`. Bounded excerpt + import so the successor can search.
  DUMP="${LBRAIN_HOME:-$HOME/.lbrain}/compaction-watcher/${SESSION:-unknown}.precompact-chat.md"
  mkdir -p "$(dirname "$DUMP")" 2>/dev/null
  python3 - "$TRANSCRIPT" "$DUMP" "$SESSION" <<'PY' >>"$LOG" 2>&1 || true
import sys
from pathlib import Path
src, dest, sid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
data = src.read_bytes()
# keep the tail (newest turns); cap ~200 KiB
cap = 200_000
tail = data[-cap:] if len(data) > cap else data
dest.write_text(
    f"# PreCompact chat excerpt\n\nsession: `{sid}`\nsource: `{src}`\nbytes_kept: {len(tail)} / {len(data)}\n\n```\n"
    + tail.decode("utf-8", "replace")
    + "\n```\n",
    encoding="utf-8",
)
PY
  # import takes a DIRECTORY — a file path yields "0 markdown files" (keel, 2026-09-01)
  timeout 90 "$LBRAIN_BIN" import "$(dirname "$DUMP")" >>"$LOG" 2>&1
fi

exit 0
