#!/usr/bin/env bash
# Release lbrain: check everything, tag, dispatch, verify. Stops loudly on any gap.
# Born from the 0.1.4 release failing once on a missing tag — the knowledge lived in a
# workflow error message; now it lives in a script that cannot skip the step.
set -euo pipefail

die() { echo "✗ $*" >&2; exit 1; }

REPO="metavolve-labs/lbrain"
REMOTE="${LBRAIN_RELEASE_REMOTE:-upstream}"

# 1. Clean tree, on main, up to date.
[ -z "$(git status --porcelain)" ] || die "working tree is dirty — release from a clean tree"
BRANCH=$(git branch --show-current)
[ "$BRANCH" = "main" ] || die "on '$BRANCH' — release from main"
git fetch "$REMOTE" --quiet
[ "$(git rev-parse HEAD)" = "$(git rev-parse "$REMOTE/main")" ] \
  || die "local main != $REMOTE/main — pull first"

# 2. Version strings agree.
V_PYPROJECT=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
V_INIT=$(grep -m1 '__version__' lbrain/__init__.py | sed 's/.*"\(.*\)".*/\1/')
[ "$V_PYPROJECT" = "$V_INIT" ] \
  || die "version mismatch: pyproject=$V_PYPROJECT __init__=$V_INIT"
V="$V_PYPROJECT"
echo "· releasing $V"

# 3. Changelog stanza exists.
grep -q "^## $V" CHANGELOG.md || die "no '## $V' stanza in CHANGELOG.md"

# 4. Not already on PyPI (a version can never be reused).
CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/lbrain/$V/json")
[ "$CODE" != "200" ] || die "lbrain $V is already on PyPI — bump the version"

# 5. Tag HEAD and push the tag (idempotent if the tag already points here).
if git rev-parse "v$V" >/dev/null 2>&1; then
  [ "$(git rev-parse "v$V^{commit}")" = "$(git rev-parse HEAD)" ] \
    || die "tag v$V exists but points elsewhere — resolve by hand"
  echo "· tag v$V already on HEAD"
else
  git tag -a "v$V" -m "lbrain $V"
  git push "$REMOTE" "v$V"
  echo "· tagged and pushed v$V"
fi

# 6. Dispatch and poll the workflow.
gh workflow run release.yml --repo "$REPO"
echo "· dispatched — polling"
sleep 20
RUN_ID=$(gh run list --repo "$REPO" --workflow=release.yml --limit 1 \
         --json databaseId --jq '.[0].databaseId')
while true; do
  S=$(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion \
      --jq '"\(.status) \(.conclusion // "")"')
  case "$S" in
    "completed success"*) echo "· workflow succeeded"; break ;;
    "completed "*)        die "workflow failed ($S) — gh run view $RUN_ID --log-failed" ;;
  esac
  sleep 20
done

# 7. Verify PyPI actually serves it, then prove a stranger install.
for _ in $(seq 1 20); do
  LIVE=$(curl -s https://pypi.org/pypi/lbrain/json \
         | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['version'])" \
         2>/dev/null || true)
  [ "$LIVE" = "$V" ] && break
  sleep 15
done
[ "$LIVE" = "$V" ] || die "PyPI still serves '$LIVE' — index not updated"
VENV=$(mktemp -d)/venv
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q "lbrain==$V"
"$VENV/bin/python" -c "import lbrain; print('· clean-venv import ok:', lbrain.__version__)"
echo "✓ lbrain $V is live and installable"
