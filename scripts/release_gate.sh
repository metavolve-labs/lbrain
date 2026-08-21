#!/bin/bash
# release_gate.sh — MUST pass before `twine upload`. No green, no publish.
#
# Every check here is a measurement of a failure that actually shipped:
#   0.1.5 published with pyproject bumped but __init__.__version__ still 0.1.4
#   0.1.5 published while a module a downstream imports existed only on one
#     machine, making that downstream unbuildable everywhere else
#   an editable checkout 141 commits off main reported as "Version: 0.1.0"
#
# Usage:  bash scripts/release_gate.sh          (from the repo root, on main)
# Exit 0 = cleared to publish. Anything else = fix it first.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
FAIL=0
say() { printf '%s\n' "$*"; }
bad() { say "✗ $*"; FAIL=1; }
ok()  { say "✓ $*"; }

say "── release gate"

# 1 · version says the same thing everywhere ─────────────────────────────────
PYPROJECT_V=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
INIT_V=$(python3 -c "import re; s=open('lbrain/__init__.py').read(); m=re.search(r'__version__\s*=\s*[\"\x27]([^\"\x27]+)', s); print(m.group(1) if m else 'MISSING')")
if [ "$PYPROJECT_V" = "$INIT_V" ]; then
  ok "version agrees: pyproject $PYPROJECT_V == __init__ $INIT_V"
else
  bad "VERSION SPLIT: pyproject says $PYPROJECT_V, lbrain/__init__.py says $INIT_V — bump BOTH (this is the 0.1.5 bug)"
fi

# 2 · release only from a clean, pushed main ─────────────────────────────────
if [ -z "$(git status --porcelain)" ]; then ok "working tree clean"; else bad "dirty working tree — a release must be reproducible from a commit"; fi
BRANCH=$(git branch --show-current)
[ "$BRANCH" = "main" ] && ok "on main" || bad "on branch '$BRANCH' — releases cut from main only"
git fetch --quiet origin 2>/dev/null || say "  (offline: skipping remote-parity check — do NOT publish offline)"
if git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  ok "HEAD is on origin/main — nothing unpushed goes into this wheel"
else
  bad "HEAD is NOT on origin/main — push first; a release from unpushed code is invisible to every other machine"
fi

# 3 · build, and prove the wheel contains what the source tree contains ──────
rm -rf dist/ && python3 -m build --quiet 2>/dev/null || python3 -m build
WHEEL=$(ls dist/*.whl | head -1)
[ -n "$WHEEL" ] && ok "built $WHEEL" || { bad "no wheel produced"; exit 1; }
SRC_MODS=$(find lbrain -name '*.py' | sort)
MISSING_MODS=""
for m in $SRC_MODS; do
  unzip -l "$WHEEL" | grep -q "$m" || MISSING_MODS="$MISSING_MODS $m"
done
if [ -z "$MISSING_MODS" ]; then
  ok "wheel carries every source module ($(echo "$SRC_MODS" | wc -l) files)"
else
  bad "WHEEL IS MISSING MODULES:$MISSING_MODS — the grading-class bug; fix packaging before publishing"
fi

# 4 · fresh-venv truth: install the wheel and measure it ─────────────────────
V=$(mktemp -d)/venv
python3 -m venv "$V" && "$V/bin/pip" install --quiet "$WHEEL"
GOT_V=$("$V/bin/python" -c "import lbrain; print(getattr(lbrain,'__version__','MISSING'))")
[ "$GOT_V" = "$PYPROJECT_V" ] && ok "clean venv imports lbrain $GOT_V" || bad "clean venv reports '$GOT_V', expected $PYPROJECT_V"
IMPORT_FAILS=$("$V/bin/python" - <<'EOF'
import importlib, pathlib, sys
fails = []
for p in sorted(pathlib.Path("lbrain").glob("*.py")):
    name = "lbrain" if p.stem == "__init__" else f"lbrain.{p.stem}"
    try: importlib.import_module(name)
    except Exception as e: fails.append(f"{name}: {e}")
print(";".join(fails))
EOF
)
[ -z "$IMPORT_FAILS" ] && ok "every public module imports from the wheel" || bad "module import failures in clean venv: $IMPORT_FAILS"
"$V/bin/lbrain" --version >/dev/null 2>&1 && ok "CLI answers --version" || bad "CLI broken in clean venv"

# 5 · downstream consumers: imports other repos depend on ────────────────────
# One import per line in release-consumers.txt (e.g. "from lbrain import grading").
if [ -f release-consumers.txt ]; then
  while IFS= read -r line; do
    [ -z "$line" ] || [ "${line:0:1}" = "#" ] && continue
    if "$V/bin/python" -c "$line" 2>/dev/null; then
      ok "consumer contract: $line"
    else
      bad "CONSUMER CONTRACT BROKEN: '$line' fails against this wheel — a downstream repo will not build"
    fi
  done < release-consumers.txt
else
  say "  (no release-consumers.txt — consumer contracts unchecked)"
fi

say ""
if [ "$FAIL" -eq 0 ]; then
  say "✅ GATE GREEN — cleared to: git tag v$PYPROJECT_V && git push origin v$PYPROJECT_V && twine upload dist/*"
else
  say "⛔ GATE RED — fix every ✗ above. Do not publish."
  exit 1
fi
