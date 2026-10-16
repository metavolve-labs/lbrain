"""The served footer names which code answered (CSO A3 parity case 2026-09-11T07:31Z: a stale MCP
process served the original defect with no signal that a repaired build existed)."""
import re

from lbrain import __version__, amp


def test_engine_stamp_is_version_plus_short_sha_from_a_checkout():
    st = amp.engine_stamp()
    assert st.startswith(__version__)
    # this test runs from the checkout, so the sha suffix must be present and 7 hex chars
    assert re.fullmatch(re.escape(__version__) + r"\+[0-9a-f]{7}", st), st


def test_provenance_footer_carries_the_stamp():
    foot = amp.provenance([], 0, 0, 0)
    assert "· engine " + amp.engine_stamp() in foot


def test_stamp_is_cached_per_process():
    assert amp.engine_stamp() is amp.engine_stamp()


def _fake_repo(tmp_path, sha="0123456789abcdef0123456789abcdef01234567", packed=False):
    common = tmp_path / "main" / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    if packed:
        (common / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/br\n")
    else:
        (common / "refs" / "heads" / "br").write_text(sha + "\n")
    return common, sha


def test_stamp_reads_a_plain_checkout(tmp_path):
    common, sha = _fake_repo(tmp_path)
    (common / "HEAD").write_text("ref: refs/heads/br\n")
    assert amp._git_head_sha(str(tmp_path / "main")) == sha


def test_stamp_follows_gitdir_in_a_worktree(tmp_path):
    """CSO PR #78 review: in a worktree `.git` is a file; the stamp used to drop the sha."""
    common, sha = _fake_repo(tmp_path)
    wtd = common / "worktrees" / "wt"
    wtd.mkdir(parents=True)
    (wtd / "HEAD").write_text("ref: refs/heads/br\n")
    (wtd / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wtd}\n")
    assert amp._git_head_sha(str(wt)) == sha


def test_stamp_falls_back_to_packed_refs(tmp_path):
    common, sha = _fake_repo(tmp_path, packed=True)
    (common / "HEAD").write_text("ref: refs/heads/br\n")
    assert amp._git_head_sha(str(tmp_path / "main")) == sha


def test_stamp_is_empty_without_a_checkout(tmp_path):
    assert amp._git_head_sha(str(tmp_path)) == ""
