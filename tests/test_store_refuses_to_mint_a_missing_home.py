"""FX-178 (2026-11-17): a read against a brain home that does not exist fails loud instead of creating it."""
import pytest
from lbrain.store import Store, HomeMissingError


def test_missing_home_is_refused_not_created(tmp_path, monkeypatch):
    monkeypatch.delenv("LBRAIN_CREATE_HOME", raising=False)
    home = tmp_path / "no-such-home"
    with pytest.raises(HomeMissingError):
        Store(home / "brain.db", embedding_dim=4)
    assert not home.exists()


def test_init_flag_allows_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_CREATE_HOME", "1")
    home = tmp_path / "new-home"
    st = Store(home / "brain.db", embedding_dim=4)
    st.close()
    assert (home / "brain.db").exists()
