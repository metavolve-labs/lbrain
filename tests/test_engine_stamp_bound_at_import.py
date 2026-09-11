"""CCO counterexample 2026-09-11T07:43Z: a stamp computed on first serve reports the HEAD at first
serve, not the HEAD the code was loaded under. This test copies the package into a scratch checkout
whose .git/HEAD is A, imports lbrain.amp there in a subprocess, moves HEAD to B inside that same
process, and asserts the stamp still says A. Then a fresh process at B says B."""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
import sys, os
sys.path.insert(0, sys.argv[1])
import lbrain.amp as amp
first = amp.engine_stamp()
open(os.path.join(sys.argv[1], ".git", "HEAD"), "w").write("b" * 40 + "\n")
print(first, amp.engine_stamp(), amp.provenance([], 0, 0, 0).rsplit("engine ", 1)[1])
"""


def _scratch(tmp_path, sha):
    co = tmp_path / "co"
    shutil.copytree(os.path.join(ROOT, "lbrain"), co / "lbrain")
    (co / ".git").mkdir(); (co / ".git" / "HEAD").write_text(sha + "\n")
    return co


def test_stamp_is_bound_at_import_not_first_serve(tmp_path):
    co = _scratch(tmp_path, "a" * 40)
    r = subprocess.run([sys.executable, "-c", PROBE, str(co)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    first, second, footer = r.stdout.split()
    assert first.endswith("+aaaaaaa") and second == first and footer == first, r.stdout


def test_fresh_process_at_new_head_reports_new_head(tmp_path):
    co = _scratch(tmp_path, "c" * 40)
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import lbrain.amp as a; print(a.engine_stamp())", str(co)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("+ccccccc")
