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
    assert st.get_meta("chunker_version") == str(CHUNKER_VERSION)
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
    assert "chunker changed" in res.output
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
    assert "chunker changed" in res.output
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
    assert "chunker changed" not in res.output
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
