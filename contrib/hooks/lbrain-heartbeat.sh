#!/usr/bin/env bash
# LBrain state heartbeat — periodic cross-session awareness (read + write).
#
# The gap this fills: SessionStart autosync reads state ONCE at startup, and
# SessionEnd/PreCompact capture writes state ONLY at the end. A long session
# therefore drifts: sibling sessions publish work (e.g. a paper, a deploy) and
# this one never notices until it ends. The heartbeat ticks mid-session.
#
# Modes:
#   --hook : Claude Code UserPromptSubmit hook. Time-gated (default 15 min via a
#            marker). When due it (1) READS — prints a STATE DELTA of lair/memory
#            files other sessions changed since the last beat, which Claude Code
#            injects into context; and (2) WRITES — refreshes the LBrain index
#            (import + small embed) so this session's own memory/lair edits become
#            visible to siblings' deep-recall.
#   --cron : wall-clock */15 cron. WRITE/refresh half only, machine-wide, so the
#            shared index stays fresh even when no active session is ticking.
#
# Design rules (same as lbrain-capture.sh): ALWAYS exit 0 (a sync problem must
# never break a session or a prompt), and every step is bounded by `timeout`.
set +e
# cron runs with a minimal PATH — make lbrain + bun reachable like the other hooks do.
export PATH="/usr/local/bin:$HOME/.bun/bin:$PATH"

MODE="${1:---hook}"
LB="${LBRAIN_BIN:-lbrain}"
LBHOME="${LBRAIN_HOME:-$HOME/.lbrain}"
LOG="$LBHOME/heartbeat.log"
GATE_SEC="${LBRAIN_HEARTBEAT_SEC:-900}"          # 15 minutes
EMBED_MAX="${LBRAIN_HEARTBEAT_EMBED_MAX:-300}"   # auto-embed at most this many stale chunks

command -v "$LB" >/dev/null 2>&1 || exit 0

# Shared state dirs to watch for the read-delta (lairs + both memory silos).
# Override with LBRAIN_WATCH_DIRS (colon-separated) if the layout changes.
DEFAULT_WATCH="$HOME/lairs:$HOME/.lbrain/memory"
IFS=':' read -r -a WATCH_DIRS <<< "${LBRAIN_WATCH_DIRS:-$DEFAULT_WATCH}"

now=$(date +%s)
due() {  # due <marker>  → 0 (true) if absent or older than the gate
  local m="$1" mt=0
  [ -f "$m" ] && mt=$(date -r "$m" +%s 2>/dev/null || echo 0)
  [ $(( now - mt )) -ge "$GATE_SEC" ]
}

# --- guard: uncommitted code must not touch the live brain (A-431) -----------
# `lbrain` is an EDITABLE install, so /usr/local/bin/lbrain imports the working
# tree, not a released wheel. This cron therefore runs whatever is on disk at the
# moment it fires. That has already reached the live brain twice in two days:
#   A-004  — a clobbered live config during a mid-edit run
#   2026-07-31 — a schema migration from an uncommitted branch applied to
#                ~/.lbrain/brain.db at 12:24, unreviewed and unchosen
# Neither was malicious or even careless; both were simply the 15-minute timer
# arriving mid-edit. The fix is to make that timing impossible rather than to
# remember not to edit near it.
#
# Refuses ONLY the automated paths. An operator running `lbrain import` by hand
# is making a choice; a cron tick is not. Opt out with LBRAIN_ALLOW_DIRTY=1.
repo_is_dirty() {
  local repo
  repo=$(timeout 5 python3 -c 'import lbrain,os;print(os.path.dirname(os.path.dirname(os.path.abspath(lbrain.__file__))))' 2>/dev/null) || return 1
  [ -n "$repo" ] && [ -d "$repo/.git" ] || return 1
  [ -n "$(timeout 10 git -C "$repo" status --porcelain 2>/dev/null)" ]
}

refresh_index() {  # import any lair/memory edits + embed a SMALL stale backlog
  if [ "${LBRAIN_ALLOW_DIRTY:-0}" != "1" ] && repo_is_dirty; then
    printf '[%s] REFUSING to refresh: the lbrain working tree is DIRTY.\n  Uncommitted code must not import the live brain (A-431). Commit, or set LBRAIN_ALLOW_DIRTY=1.\n' \
      "$(date -Is)" >>"$LOG" 2>&1
    return 0
  fi
  timeout 60 "$LB" import >>"$LOG" 2>&1
  local stale
  stale=$("$LB" stats 2>/dev/null | awk -F'[: ]+' '/^chunks:/{c=$2}/^embedded:/{e=$2}END{print (c+0)-(e+0)}')
  if [ "${stale:-0}" -gt 0 ] && [ "${stale:-0}" -le "$EMBED_MAX" ]; then
    timeout 180 "$LB" embed --stale >>"$LOG" 2>&1
  fi
}

emit_delta() {  # print lair/memory files changed since <ref>, for context injection
  local ref="$1" out="" base desc
  for d in "${WATCH_DIRS[@]}"; do
    [ -d "$d" ] || continue
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      base=$(basename "$f")
      [ "$base" = "MEMORY.md" ] && continue   # the index file itself is noise here
      desc=$(grep -m1 '^description:' "$f" 2>/dev/null | sed 's/^description:[[:space:]]*//;s/^["'\'']//;s/["'\'']$//' | cut -c1-110)
      if [ -n "$desc" ]; then out+="  • ${base} — ${desc}"$'\n'; else out+="  • ${base}"$'\n'; fi
    done < <(find "$d" -name '*.md' -newer "$ref" 2>/dev/null | head -25)
  done
  [ -z "$out" ] && return 0
  printf '⟳ LBrain heartbeat — shared lair/memory files OTHER sessions changed since your last sync.\nTreat as fresh ground truth (newest-timestamp-wins); re-read before acting on related work:\n%s\n' "$out"
}

case "$MODE" in
  --hook)
    MARKER="$LBHOME/.heartbeat"
    due "$MARKER" || exit 0
    # READ (synchronous — fast find+grep — so stdout is injected into context):
    # only diff if we have a prior marker to compare against.
    [ -f "$MARKER" ] && emit_delta "$MARKER"
    # WRITE (detached — import can take ~10s; must NOT add latency to the prompt):
    # re-invoke self fully detached so this session's edits reach siblings' index.
    setsid "$0" --refresh-bg >/dev/null 2>&1 < /dev/null &
    touch "$MARKER" 2>/dev/null || true
    ;;
  --refresh-bg)  # internal: detached index refresh launched by --hook
    refresh_index
    ;;
  --cron)
    MARKER="$LBHOME/.heartbeat-cron"
    due "$MARKER" || exit 0
    refresh_index
    touch "$MARKER" 2>/dev/null || true
    ;;
  *)
    echo "usage: lbrain-heartbeat.sh [--hook|--cron]" >&2
    ;;
esac
exit 0
