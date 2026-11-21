#!/usr/bin/env python3
"""NC-14 - negative control for the capture-hook text extraction (Tier 0).

The accidental-pass risk: a checker that merely confirms "output is non-empty" passes for the
OLD byte-slice too. The load-bearing arms are the ones where old and new must DIFFER.
"""
import json, re, subprocess, sys, tempfile, glob, os
from pathlib import Path

REAL = sorted(glob.glob('/root/.claude/projects/*/*.jsonl'), key=os.path.getsize)
REAL = [f for f in REAL if os.path.getsize(f) > 400_000]
if not REAL:
    print("NO REAL TRANSCRIPT >400KB — cannot run"); sys.exit(2)
src = REAL[-1]

def payload_share(text):
    """Share of text that is ENCODED PAYLOAD, measured as a substring.

    Two wrong versions preceded this one, and both are the day's own lesson:
      v1 measured any run of 60+ non-space chars -- a PROXY. On the repaired output its
         residual was 95 file paths and 57 long mail filenames: 7% false positive, 0% real.
      v2 required a WHOLE run to be base64 -- but in raw JSONL the payload sits inside a
         larger token ('"signature":"AAA..."'), so it scored the untouched defect at 0%.
    The property is: a BLOCK of encoded payload, distinguished from a quoted digest.
    Measured on the repaired output, the residual encoded substrings are 100% sha256
    digests QUOTED IN OUR OWN PROSE ("Result `X.md` sha256 2e57...") -- evidence pins a
    reader needs, not payload. A digest is exactly 64 chars; a thinking-block signature
    is thousands. Length is the discriminator, and it is measured rather than chosen."""
    enc = re.findall(r'[A-Za-z0-9+/]{100,}={0,2}', text)
    return sum(len(r) for r in enc) / max(len(text), 1)


# --- OLD implementation, verbatim from HEAD ---
def old(srcp):
    data = Path(srcp).read_bytes()
    tail = data[-200_000:] if len(data) > 200_000 else data
    return tail.decode("utf-8", "replace")

# --- NEW implementation, via the live hook ---
def new(srcp):
    script = Path("contrib/hooks/lbrain-capture.sh").read_text()
    body = script.split("python3 - \"$TRANSCRIPT\" \"$DUMP\" \"$SESSION\" <<'PY'")[1].split("\nPY\n")[0]
    body = body.split("\n", 1)[1]        # drop the heredoc opener's trailing shell redirect
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "o.md"
        subprocess.run([sys.executable, "-c", body, srcp, str(out), "sid"], check=True)
        return out.read_text()

o, n = old(src), new(src)
po, pn = payload_share(o), payload_share(n)
pass_ct = fail_ct = 0
def ok(name, cond):
    global pass_ct, fail_ct
    print(("PASS " if cond else "FAIL ") + name); 
    globals().__setitem__('pass_ct', pass_ct + (1 if cond else 0))
    globals().__setitem__('fail_ct', fail_ct + (0 if cond else 1))

print(f"  source: {os.path.basename(src)}  {os.path.getsize(src):,} bytes")
print(f"  OLD payload share {po:.1%}   NEW payload share {pn:.1%}")
print()
ok(f"ARM1 OLD output carries encoded PAYLOAD BLOCKS -- the defect reproduces [{po:.1%}]", po > 0.05)
ok(f"ARM2 NEW output carries NO payload block [{pn:.2%}]", pn == 0.0)
ok("ARM2b NEW RETAINS quoted sha256 digests (evidence pins survive extraction)",
   len(re.findall(r'\b[0-9a-f]{64}\b', n)) > 5)
ok("ARM3 NEW output is non-empty prose", len(n) > 2000)
ok("ARM4 NEW emits NO U+FFFD replacement chars", "�" not in n)
ok("ARM5 OLD first line is a truncated fragment; NEW starts at a record boundary",
   (lambda first: not first.strip().startswith("#"))(o.splitlines()[0] if o.splitlines() else "")
   and n.lstrip().startswith("# PreCompact"))
ok("ARM6 NEW declares its extraction method (a reader can tell what was dropped)",
   "extraction: parsed JSONL" in n)
# anti-trivial: a stub that echoes the raw tail must FAIL arm 2
stub_payload = payload_share(old(src))
ok("ARM2x raw-tail countermodel FAILS arm 2 (so arm 2 is not vacuous)", not (stub_payload < 0.05))
print()
print(f"NC-14: {pass_ct} passed, {fail_ct} failed")
sys.exit(1 if fail_ct else 0)
