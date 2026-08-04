"""A-435 — a chunker upgrade must reach the DATA, not just the code.

`import` short-circuits on the body hash. Correct for content, blind to the
algorithm: shipping new chunk boundaries left every existing corpus on the old
ones, silently, with no way for a user to notice their index was built by code
they no longer run. `installed` != `applied` — doctrine rule 2, one layer down.
"""
from click.testing import CliRunner

from lbrain.cli import main
from lbrain.index import CHUNKER_VERSION
from lbrain.store import Store


def _corpus(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# A\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    return src


def _run(home, monkeypatch, *args):
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import importlib
    import lbrain.config
    importlib.reload(lbrain.config)
    return CliRunner().invoke(main, list(args))


def test_first_import_stamps_the_version(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    _run(home, monkeypatch, "import", str(_corpus(tmp_path)))
    st = Store(home / "brain.db", embedding_dim=384)
    # Composite: algorithm PLUS every config input to chunk boundaries.
    stamp = st.get_meta("chunker_version")
    assert stamp.startswith(f"{CHUNKER_VERSION}:"), stamp
    assert len(stamp.split(":")) == 4, stamp
    st.close()


def test_unversioned_brain_is_treated_as_stale(tmp_path, monkeypatch):
    """The population that MOST needs re-chunking carries no version at all.

    Treating unknown as current would make the guard a no-op on every existing
    install while passing on a fresh one — the incomplete-fix shape of A-005/A-404.
    """
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))

    st = Store(home / "brain.db", embedding_dim=384)
    st.db.execute("DELETE FROM meta WHERE key = 'chunker_version'")
    st.db.commit(); st.close()

    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" in res.output
    assert "unversioned" in res.output


def test_version_bump_forces_rechunk(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))

    st = Store(home / "brain.db", embedding_dim=384)
    st.set_meta("chunker_version", "1")
    st.close()

    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" in res.output
    assert "updated: 1" in res.output or "new: 1" in res.output


def test_same_version_does_not_rechunk(tmp_path, monkeypatch):
    """The negative control — the guard must not re-embed on every run forever.

    A-003 was exactly this: a detector that fired on every import, costing real
    money to change nothing.
    """
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))
    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" not in res.output
    assert "unchanged: 1" in res.output


def test_hosted_provider_upgrade_warns_that_it_COSTS(tmp_path, monkeypatch):
    """An upgrade that quietly bills is the plausible-default failure at its worst.

    On-device re-embedding is time. A hosted provider is money the user did not
    ask to spend, and the warning must name that before they run embed --stale.
    """
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text(
        'embedding_provider = "gemini"\nembedding_dim = 1536\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))
    st = Store(home / "brain.db", embedding_dim=1536)
    st.set_meta("chunker_version", "1"); st.close()
    res = _run(home, monkeypatch, "import", str(src))
    assert "BILLED API call" in res.output
    assert "keeps working on the old vectors" in res.output


def test_local_provider_upgrade_says_it_is_free(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))
    st = Store(home / "brain.db", embedding_dim=384)
    st.set_meta("chunker_version", "1"); st.close()
    res = _run(home, monkeypatch, "import", str(src))
    assert "costs time, not money" in res.output
    assert "BILLED" not in res.output


def test_rechunk_flag_forces_it_without_a_version_change(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))
    res = _run(home, monkeypatch, "import", "--rechunk", str(src))
    assert "unchanged: 1" not in res.output


# --- the gaps an adversarial audit found in the fix above (2026-08-01) --------

def test_changing_chunk_tokens_forces_a_rechunk(tmp_path, monkeypatch):
    """Config is an INPUT to chunk boundaries, so it belongs in the fingerprint.

    Shipped as a bare int, which left chunk_tokens/chunk_overlap/contextual_prefix
    outside it: halving chunk_tokens printed `unchanged: 1` and produced 512-token
    chunks forever, with the new value reported [config] by every reader.
    """
    home = tmp_path / "h"; home.mkdir()
    cfgp = home / "config.toml"
    cfgp.write_text('embedding_provider = "local"\nchunk_tokens = 512\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))

    cfgp.write_text('embedding_provider = "local"\nchunk_tokens = 64\n', encoding="utf-8")
    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" in res.output
    assert "unchanged: 1" not in res.output


def test_enabling_contextual_prefix_forces_a_rechunk(tmp_path, monkeypatch):
    home = tmp_path / "h"; home.mkdir()
    cfgp = home / "config.toml"
    cfgp.write_text('embedding_provider = "local"\ncontextual_prefix = false\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))

    cfgp.write_text('embedding_provider = "local"\ncontextual_prefix = true\n', encoding="utf-8")
    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" in res.output


def test_stored_20_is_not_read_as_current_against_version_2(tmp_path, monkeypatch):
    """`startswith` made "20" look current against 2 — a prefix compare on a
    version number is a bug waiting for the tenth release."""
    home = tmp_path / "h"; home.mkdir()
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "import", str(src))
    st = Store(home / "brain.db", embedding_dim=384)
    st.set_meta("chunker_version", "20:512:64:0"); st.close()
    res = _run(home, monkeypatch, "import", str(src))
    assert "chunking changed" in res.output


def test_partial_import_does_not_stamp_the_whole_brain(tmp_path, monkeypatch):
    """`lbrain import <one-dir>` re-chunks one source. Stamping globally would
    permanently strand the others on old boundaries with nothing left to detect it."""
    home = tmp_path / "h"; home.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "x.md").write_text("# X\n\nbody\n", encoding="utf-8")
    (home / "config.toml").write_text(
        f'embedding_provider = "local"\nsources = ["{a.as_posix()}", "{b.as_posix()}"]\n',
        encoding="utf-8")
    _run(home, monkeypatch, "import")           # full pass -> stamped
    st = Store(home / "brain.db", embedding_dim=384)
    st.set_meta("chunker_version", "1"); st.close()

    res = _run(home, monkeypatch, "import", str(a))   # partial
    assert "partial import" in res.output
    st = Store(home / "brain.db", embedding_dim=384)
    assert st.get_meta("chunker_version") == "1", "partial import stamped the brain current"
    st.close()


# --- A-517: `doctor` reported the embedding fingerprint and NOT the chunker one ---
# So a v2 index under v3 code passed `doctor` with a clean bill of health. A-435
# closed this for `import`, which ACTS on the mismatch; the command an operator
# actually runs to ask "is my index sound?" never mentioned it. Same blind spot,
# one layer up: the guard existed, the place people look didn't have it.

def test_doctor_reports_chunker_drift_instead_of_a_clean_bill_of_health(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "init", "--source", str(src), "-y")
    _run(home, monkeypatch, "import")

    # Age the index to a previous chunker, exactly as a real upgrade would.
    store = Store(home / "brain.db", embedding_dim=384)
    store.set_meta("chunker_version", f"{CHUNKER_VERSION - 1}:512:64:0")
    store.db.commit()
    store.close()

    res = _run(home, monkeypatch, "doctor")
    assert "CHUNKER DRIFT" in res.output, res.output
    assert f"{CHUNKER_VERSION - 1}:512:64:0" in res.output
    assert "lbrain import" in res.output, "must say what to RUN, not just that it is wrong"


def test_doctor_confirms_a_matching_chunker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "init", "--source", str(src), "-y")
    _run(home, monkeypatch, "import")
    res = _run(home, monkeypatch, "doctor")
    assert "CHUNKER DRIFT" not in res.output
    assert "built by this chunker" in res.output, res.output


def test_chunker_drift_does_not_change_doctors_exit_contract(tmp_path, monkeypatch):
    """`doctor` exits non-zero when the stored VECTORS cannot be trusted. Chunker
    drift is a weaker claim — stale, not wrong — and `import` repairs it. Widening
    the exit code would start failing every script that gates on `doctor`."""
    home = tmp_path / "home"
    src = _corpus(tmp_path)
    _run(home, monkeypatch, "init", "--source", str(src), "-y")
    _run(home, monkeypatch, "import")
    store = Store(home / "brain.db", embedding_dim=384)
    store.set_meta("chunker_version", "1 (unversioned)")
    store.db.commit()
    store.close()
    res = _run(home, monkeypatch, "doctor")
    assert "CHUNKER DRIFT" in res.output
    assert res.exit_code == 0, "chunker drift must warn, not gate"


def test_doctor_and_import_share_one_fingerprint_implementation(tmp_path, monkeypatch):
    """Two copies of the id string would drift apart and each look right alone."""
    from lbrain.index import chunker_fingerprint
    assert chunker_fingerprint(512, 64, False) == f"{CHUNKER_VERSION}:512:64:0"
    assert chunker_fingerprint(512, 64, True) == f"{CHUNKER_VERSION}:512:64:1"
