"""Every tracked file that starts with a shebang is tracked executable (mode 100755).

FX-050, 2026-11-13: commit d72cf84 replaced the heartbeat hook by write-and-rename; the new file took the default mode,
git recorded 100644, and cron and the seat's hook both called it by path and failed silently for 13 hours (restored in
a16bd06). Publishing by rename is right; not re-checking the mode was the fault. This test is the re-check: it fails
the suite whoever wrote the file and whatever tool they used.
"""
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_every_tracked_shebang_file_is_tracked_executable():
    try:
        out = subprocess.run(["git", "ls-files", "-s"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    bad = []
    for line in out.splitlines():
        meta, _, rel = line.partition("\t")
        if not meta.startswith("100644"):
            continue
        try:
            with open(REPO / rel, "rb") as fh:
                if fh.read(2) == b"#!":
                    bad.append(rel)
        except OSError:
            continue
    assert not bad, "tracked with a shebang but not executable (git update-index --chmod=+x <file>): " + ", ".join(bad)
