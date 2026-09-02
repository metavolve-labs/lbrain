#!/usr/bin/env bash
# release-preflight.sh — run BEFORE tagging or dispatching a release. Every check
# below is a scar with a date, not a best practice:
#
#   0.1.9/0.1.9.post1 (2026-09-01): published over RED CI; shipped without the
#     fixes its PR named; 0.1.9 self-reported 0.1.8.
#   0.1.10 (2026-09-02): shipped self-reporting 0.1.9.post1 — the drift test was
#     RED in CI and the publish path never reads CI; verification checked
#     importlib.metadata (right) and never --version (wrong).
#
# Tad, 2026-09-02: "Two misfired versioning pushes in a row. Slow it down.
# Create a preflight for version pushes and follow it." This is that preflight.
# It REFUSES loudly, prints exactly what to do next on success, and requires the
# CSO artifact verdict as an ARGUMENT — verification is an input to a release,
# not a courtesy after one (VD-01).
#
#   scripts/release-preflight.sh --cso-verdict <mail-path-or-ref>
#   scripts/release-preflight.sh --pre-verdict   # everything EXCEPT the verdict
#                                                # gate, to prepare artifacts FOR
#                                                # the CSO to verify
set -uo pipefail
cd "$(dirname "$0")/.."

CSO_VERDICT=""
PRE_VERDICT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --cso-verdict) CSO_VERDICT="${2:?--cso-verdict needs a reference}"; shift 2;;
    --pre-verdict) PRE_VERDICT=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

FAIL=0
ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*" >&2; FAIL=1; }
hdr() { echo; echo "── $*"; }

PKG=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
HEAD_SHA=$(git rev-parse HEAD)
echo "release-preflight — candidate $PKG at ${HEAD_SHA:0:9}"

hdr "1 · the 2026-08-19 release gate (Tad's standing rule — it existed and BOTH misfires skipped it)"
# release_gate.sh already asserts: version declarations agree · clean tree · ON
# MAIN · HEAD pushed to origin/main · wheel module inventory · fresh-venv
# imports · CLI answers · release-consumers.txt contracts. Those scars stay its.
if bash scripts/release_gate.sh; then
  ok "release_gate.sh GREEN"
else
  bad "release_gate.sh RED — its ✗ lines above are the oldest law here; fix them first"
fi

hdr "2 · version is unused on PyPI (an upload can never be undone)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/lbrain/$PKG/json")
[ "$CODE" = "404" ] && ok "lbrain $PKG not on PyPI (HTTP 404)" \
  || bad "PyPI answers HTTP $CODE for $PKG — already used or index unreachable; a version number is burned forever"

hdr "3 · CI is GREEN on THIS exact commit (0.1.9.x + 0.1.10 were published past red CI)"
CONC=$(gh run list --repo metavolve-labs/lbrain --workflow CI --limit 15 \
        --json headSha,conclusion,status \
        --jq "[.[] | select(.headSha==\"$HEAD_SHA\")][0] | (.conclusion // .status)" 2>/dev/null)
case "$CONC" in
  success) ok "CI success on ${HEAD_SHA:0:9}";;
  "")      bad "NO CI run found for ${HEAD_SHA:0:9} — wait for it; absence is not green";;
  *)       bad "CI on ${HEAD_SHA:0:9} is '$CONC' — a red or pending CI blocks release, no exceptions";;
esac

hdr "4 · RELEASE-REQUIRES ancestry (the 0.1.9 lacked-its-own-fixes class)"
REQ=0
while read -r sha label; do
  case "$sha" in ''|'#'*) continue;; esac
  if git merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
    ok "contains $sha ($label)"; REQ=$((REQ+1))
  else
    bad "HEAD lacks required fix $sha ($label)"
  fi
done < <(cat RELEASE-REQUIRES 2>/dev/null; echo)
echo "  required fixes verified: $REQ"

hdr "5 · full suite, warm AND forced-cold (the fastembed class)"
TMPH=$(mktemp -d)
if LBRAIN_HOME="$TMPH" python3 -m pytest tests/ -q >/dev/null 2>&1; then ok "suite green (warm)"; else bad "suite RED (warm) — run it visibly and fix"; fi
if LBRAIN_TEST_FORCE_COLD=1 LBRAIN_HOME="$TMPH" python3 -m pytest tests/ -q >/dev/null 2>&1; then ok "suite green (forced cold)"; else bad "suite RED (forced cold) — this is what CI cold sees"; fi

hdr "6 · build + the artifact agrees with ITSELF + installs + all THREE surfaces"
rm -rf dist && python3 -m build >/dev/null 2>&1 || bad "python -m build failed"
WHL=$(ls dist/*.whl 2>/dev/null | head -1)
if [ -n "$WHL" ]; then
  SELF=$(python3 -c "
import re, zipfile, sys
z = zipfile.ZipFile('$WHL')
m = re.search(r'__version__\s*=\s*\"([^\"]+)\"', z.read('lbrain/__init__.py').decode())
print(m.group(1) if m else '(missing)')")
  [ "$SELF" = "$PKG" ] && ok "wheel __version__ $SELF == $PKG" \
    || bad "WHEEL MISREPORTS ITSELF: __version__ '$SELF' vs metadata '$PKG' (the 0.1.10 defect, in your hands before upload)"
  VENV=$(mktemp -d)/venv
  python3 -m venv "$VENV" >/dev/null 2>&1 && "$VENV/bin/pip" install -q "$WHL" >/dev/null 2>&1
  V1=$("$VENV/bin/python" -c "import lbrain; print(lbrain.__version__)" 2>/dev/null)
  V2=$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('lbrain'))" 2>/dev/null)
  V3=$("$VENV/bin/lbrain" --version 2>/dev/null | grep -o '[0-9][^ ]*$')
  if [ "$V1" = "$PKG" ] && [ "$V2" = "$PKG" ] && [ "$V3" = "$PKG" ]; then
    ok "installed wheel: __version__/$V1 · metadata/$V2 · --version/$V3 all agree"
  else
    bad "installed surfaces disagree: __version__='$V1' metadata='$V2' --version='$V3' expected '$PKG' — check the surface that is WRONG, not the one that is right"
  fi
  LBRAIN_HOME=$(mktemp -d) "$VENV/bin/lbrain" selftest >/dev/null 2>&1 && ok "selftest passes from the installed wheel" \
    || bad "selftest FAILS from the installed wheel"
else
  bad "no wheel in dist/"
fi

hdr "7 · CSO artifact verdict (VD-01: verification is an INPUT to release)"
if [ "$PRE_VERDICT" = "1" ]; then
  echo "  ‣ --pre-verdict: artifacts in dist/ are ready to hand to the CSO:"
  sha256sum dist/* 2>/dev/null | sed 's|dist/|      |'
  echo "  ‣ file the workorder, get the verdict, re-run WITH --cso-verdict <ref>."
  FAIL=1  # pre-verdict mode never authorizes a release
elif [ -n "$CSO_VERDICT" ]; then
  ok "CSO verdict referenced: $CSO_VERDICT (the reference goes in the tag message — auditably)"
else
  bad "no --cso-verdict given. A release without an independent artifact verdict is how both misfires shipped. Use --pre-verdict to prepare artifacts for the CSO."
fi

echo
if [ "$FAIL" -eq 0 ]; then
  cat <<DONE
✅ PREFLIGHT CLEAR for lbrain $PKG at ${HEAD_SHA:0:9}. The release sequence, in order:
   1. git tag -a v$PKG -m "lbrain $PKG — <summary>; CSO verdict: $CSO_VERDICT; preflight clear at ${HEAD_SHA:0:9}"
   2. git push origin v$PKG
   3. gh workflow run release.yml --repo metavolve-labs/lbrain --ref v$PKG   # AT the tag, always
   4. gh run watch <run-id> — do not walk away; a skipped watch is how run 33593710375's lesson got learned
   5. Verify FROM PyPI in a fresh venv: index latest + __version__ + --version + metadata + selftest
   6. Receipt to both CSO instances with what you measured
DONE
else
  echo "⛔ PREFLIGHT REFUSED — fix every ✗ above. Slowing down is the feature." >&2
fi
exit "$FAIL"
