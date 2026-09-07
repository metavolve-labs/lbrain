"""CSO/katana falsification of PR #57 fix A (2026-09-07): on POSIX, os.path.normcase is the
identity, so _norm_path folds nothing on DrvFs (WSL) or APFS (macOS) — the two filesystems the
fix names. The PR's live test never runs on either box because its probe uses tempfile.mkdtemp()
(ext4 /tmp on WSL), not the pytest basetemp. This test probes the ACTUAL tmp_path filesystem.
Run with --basetemp on a case-insensitive fs: RED on ba3f2b2."""
import os
import pytest

from lbrain.index import parse
from lbrain.store import Store


def _ci(tmp_path) -> bool:
    (tmp_path / "CaSe.probe").write_text("x")
    return (tmp_path / "case.probe").exists()


def test_case_differing_root_live_on_this_fs(tmp_path):
    if not _ci(tmp_path):
        pytest.skip("tmp_path fs is case-sensitive; pass --basetemp on DrvFs/APFS")
    rootA = tmp_path / "Lairs"
    rootA.mkdir()
    p = rootA / "live.md"
    p.write_text("# Live\nreachable content one two three\n")
    st = Store(tmp_path / "brain.db")
    st.upsert_doc(parse(p, repo_root=rootA))
    st.db.commit()
    # swap case of the LAST component only: swapping the whole path walks off the
    # case-insensitive mount (e.g. /mnt on WSL is ext4, so /MNT does not exist)
    swapped = str(tmp_path / "lAIRS")
    assert os.path.isdir(swapped) and os.path.samefile(swapped, rootA)
    would = st.prune_unreachable(source_roots=[swapped], dry_run=True)
    assert would == [], f"present, reachable doc reported unreachable via case-differing root: {would}"
