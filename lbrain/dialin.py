"""The Dial-In — one-time, agent-led LBrain setup.

`lbrain init` gets keys and config; `lbrain onboard` scaffolds starter lairs. Neither
wires the HARNESS: the recall-first habit, auto re-sync after edits, MCP registration,
memory placement. The dial-in ships that as a prompt, not an integration: the user's
own agent runs the interview and performs only the additive steps, recording each one
in a manifest so setup is auditable, idempotent, and reversible.

Design rules (from the onboarding lair, 2026-08-09):
- Every question has a default; Enter-Enter-Enter must produce a working setup.
- The agent performs ADDITIVE steps only; destructive or credential-shaped actions are
  shown to the human, never run.
- Defaults for external identifiers are EMPTY and fail-loud, never plausible.
- The dial-in ends with a receipt (`lbrain doctor` + one live query round-trip),
  not an assumption.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path


def _config_dir() -> Path:
    # Late lookup, never a module-level bind: test isolation repoints
    # config.CONFIG_DIR, and a Path captured at import time would dodge it —
    # the exact mechanism that destroyed a live credential on 2026-08-01.
    from . import config
    return config.CONFIG_DIR


def manifest_path() -> Path:
    return _config_dir() / "setup-manifest.md"


# --------------------------------------------------------------------------- #
# The interview                                                               #
# --------------------------------------------------------------------------- #

INTERVIEW_PROMPT = """\
LBRAIN DIAL-IN — one-time setup interview (instructions for the user's AI agent)

You are the user's agent. Ask the questions below ONE AT A TIME, in order. Every
question has a default — if the user just says "yes" or presses on, take the default
and move to the next. Accepting all defaults should take under a minute.

Binding rules for you, the agent:
- Perform ADDITIVE steps only. Anything destructive, credential-shaped, or paid:
  print the exact command for the human to run themselves.
- After each step you actually perform, record it:
    lbrain setup record <kind> "<what you did>" --path <artifact> --undo "<undo command>"
  (kinds: hook | mcp | source | memory | core | other)
- Never invent an external identifier (email, URL, endpoint, key name). If one is
  needed and not supplied, leave it empty and say so out loud.
- If a step fails, say so plainly and continue; never report the setup as complete
  with a failed step unmentioned.

THE QUESTIONS

1. SOURCES — "Where does your knowledge live? (folders of notes, docs, repos)"
   Default: the current directory.
   Then: `lbrain add-source <dir>` for each, followed by `lbrain import <dir>`.

2. EMBEDDINGS — "On-device embeddings (private, no key, default) or a hosted
   provider (needs an API key)?"
   Default: on-device. Then: `lbrain init` with the chosen provider. If hosted,
   the HUMAN pastes the key — you never handle it.

3. RECALL-FIRST HOOK — "Install a gentle hook that reminds your agent to check
   LBrain before grepping your notes or asserting from memory?"
   Default: yes (warn-only; strict blocking is an opt-in via LBRAIN_FIRST_MODE=block).
   Then: write the template from `lbrain setup templates`, wire it into the harness.

4. AUTO RE-SYNC — "Re-index automatically after you edit indexed files?"
   Default: yes. Then: install the autosync hook template (runs
   `lbrain import` + `lbrain embed --stale` after edits under your source dirs).

5. AGENT MEMORY — "Should I save my session notes somewhere LBrain indexes?"
   Default: yes. Then: create a memory folder, `lbrain add-source` it, and note the
   convention in your project instructions file.

6. CORE MEMORY — "What should ALWAYS be in context? (who you are, the project,
   standing rules — 3 lines)"
   Default: skip. Then: seed CORE.md in the LBrain home with what they give you.

7. MCP — "Register LBrain's MCP server with this harness so I can query it as a
   tool?" Default: yes if the harness supports MCP.
   Then (Claude Code): `claude mcp add lbrain -- lbrain mcp`

8. SECRET HYGIENE — "Enable the secret scan on import?" Default: yes.
   Teach the rule either way: anything indexed is SERVED to whatever queries the
   brain — point at secrets, never paste them into indexed files.

9. HISTORY IMPORT — "Want to seed the brain from your existing AI chat history?
   I can hand you an export prompt for the assistant you already use."
   Default: offer, don't push.

FINISH — THE RECEIPT (required, never skipped)
- Run `lbrain doctor` and show the user the result.
- Run one real `lbrain query "<something from their own material>"` and one
  `lbrain search "<a literal string they know is in there>"`, and show both.
- Tell the user: `lbrain setup status` lists everything installed and how to undo it.
- Optional, last, never a gate: a permanent gcx:// identity can be bound later with
  `lbrain register` — the local brain is complete without it.
"""


# --------------------------------------------------------------------------- #
# Hook templates (Claude Code first; harness-agnostic bash)                    #
# --------------------------------------------------------------------------- #

HOOK_RECALL_FIRST = """\
#!/usr/bin/env bash
# lbrain-recall-first.sh — PreToolUse hook: nudge recall before raw search.
#
# Fires when the agent greps/rgs YOUR knowledge dirs instead of asking LBrain.
# Default is WARN (a reminder in context, the command still runs). Set
# LBRAIN_FIRST_MODE=block to make it a hard gate with the LBRAIN_OK=1 escape —
# the strict posture; earn it after the habit exists.
#
# Replace __LBRAIN_SOURCE_DIRS__ with an ERE matching your indexed dirs,
# e.g.  my-notes|second-brain|docs/decisions
set -uo pipefail

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | python3 -c "
import sys,json
try: print(json.load(sys.stdin).get('tool_input',{}).get('command',''))
except Exception: print('')
" 2>/dev/null)

[ -z "$CMD" ] && exit 0
printf '%s' "$CMD" | grep -qE '(^|[|;&[:space:]])(grep|rg|ag|ack)([[:space:]]|$)' || exit 0
printf '%s' "$CMD" | grep -qE '__LBRAIN_SOURCE_DIRS__' || exit 0
printf '%s' "$CMD" | grep -q 'LBRAIN_OK' && exit 0

cat >&2 <<'MSG'

  lbrain: this searches your indexed knowledge. Recall answers more than grep:
     lbrain query  "<the question in natural language>"
     lbrain search "<the literal string>"
  grep finds where a string is; recall knows what you decided, when, and
  whether it was superseded. Already recalled? prefix: LBRAIN_OK=1 <command>

MSG

if [ "${LBRAIN_FIRST_MODE:-warn}" = "block" ]; then
  exit 2
fi
exit 0
"""

HOOK_AUTOSYNC = """\
#!/usr/bin/env bash
# lbrain-autosync.sh — PostToolUse hook: re-index after edits to indexed files.
#
# Runs `lbrain import` + `lbrain embed --stale` in the background when the agent
# writes/edits a file under your source dirs. A lock file keeps concurrent edits
# from piling up imports. Replace __LBRAIN_SOURCE_DIRS__ as in the recall hook.
set -uo pipefail

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c "
import sys,json
try: print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))
except Exception: print('')
" 2>/dev/null)

[ -z "$FILE" ] && exit 0
printf '%s' "$FILE" | grep -qE '__LBRAIN_SOURCE_DIRS__' || exit 0

LOCK="${TMPDIR:-/tmp}/lbrain-autosync.lock"
if mkdir "$LOCK" 2>/dev/null; then
  (
    trap 'rmdir "$LOCK"' EXIT
    lbrain import "$(dirname "$FILE")" >/dev/null 2>&1
    lbrain embed --stale >/dev/null 2>&1
  ) &
fi
exit 0
"""

CLAUDE_SETTINGS_SNIPPET = """\
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "%(dir)s/lbrain-recall-first.sh"}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {"type": "command", "command": "%(dir)s/lbrain-autosync.sh"}
        ]
      }
    ]
  }
}
"""

TEMPLATES_README = """\
# LBrain dial-in templates

Written by `lbrain setup templates`. Before wiring anything in:

1. Replace `__LBRAIN_SOURCE_DIRS__` in BOTH hook scripts with an ERE matching
   your indexed directories (the ones you `lbrain add-source`d).
2. Claude Code: merge `claude-code-settings-snippet.json` into your project's
   `.claude/settings.json` (or add the hooks via /config). Other harnesses: call
   the scripts from the equivalent pre/post tool events.
3. The recall hook default is WARN. `LBRAIN_FIRST_MODE=block` in your environment
   makes it a hard gate with the `LBRAIN_OK=1` escape prefix.
4. Record what you installed so it stays auditable:
   `lbrain setup record hook "recall-first hook" --path <script> --undo "rm <script>"`
"""


def write_templates(target: Path) -> list[Path]:
    """Write hook + harness templates into `target`. Idempotent overwrite."""
    target.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body, execbit in (
        ("lbrain-recall-first.sh", HOOK_RECALL_FIRST, True),
        ("lbrain-autosync.sh", HOOK_AUTOSYNC, True),
        ("claude-code-settings-snippet.json",
         CLAUDE_SETTINGS_SNIPPET % {"dir": str(target)}, False),
        ("README.md", TEMPLATES_README, False),
    ):
        p = target / name
        p.write_text(body, encoding="utf-8")
        if execbit:
            p.chmod(p.stat().st_mode | 0o111)
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# The manifest — what the dial-in installed, and how to undo it                #
# --------------------------------------------------------------------------- #

MANIFEST_HEADER = """\
# LBrain setup manifest

Written by the dial-in (`lbrain setup`). One line per step actually performed.
This file is the undo path and the audit trail: `lbrain setup status` reads it,
and `lbrain doctor` warns when a recorded artifact has vanished (drift).

"""

_ENTRY_RE = re.compile(
    r"^- ts=(?P<ts>\S+) kind=(?P<kind>\S+) path=(?P<path>\S+) \| "
    r"(?P<desc>.*?) \| undo: (?P<undo>.*)$"
)

VALID_KINDS = ("hook", "mcp", "source", "memory", "core", "other")


def record_step(kind: str, desc: str, path: str | None = None,
                undo: str | None = None) -> bool:
    """Append a step to the manifest. Returns False (and writes nothing) if an
    entry with the same kind+path+desc already exists — re-running the dial-in
    repairs, never duplicates."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    mp = manifest_path()
    entries = read_manifest()
    for e in entries:
        if (e["kind"], e["path"], e["desc"]) == (kind, path or "-", desc):
            return False
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"- ts={ts} kind={kind} path={path or '-'} | {desc} | undo: {undo or '(none recorded)'}\n"
    mp.parent.mkdir(parents=True, exist_ok=True)
    if not mp.exists():
        mp.write_text(MANIFEST_HEADER, encoding="utf-8")
    with mp.open("a", encoding="utf-8") as f:
        f.write(line)
    return True


def read_manifest() -> list[dict]:
    mp = manifest_path()
    if not mp.exists():
        return []
    entries = []
    for raw in mp.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(raw)
        if m:
            entries.append(m.groupdict())
    return entries


def drift_check() -> list[str]:
    """Recorded artifacts that no longer exist. A warning, not an error: the
    user may have removed them deliberately — but silently diverging from the
    manifest is the failure mode this file exists to prevent."""
    warnings = []
    for e in read_manifest():
        p = e["path"]
        if p != "-" and not Path(p).expanduser().exists():
            warnings.append(
                f"{e['kind']} recorded at {p} is GONE (was: {e['desc']}) — "
                f"remove the manifest line if intentional")
    return warnings
