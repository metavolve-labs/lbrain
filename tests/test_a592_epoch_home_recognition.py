"""A-592 — an epoch-managed home whose epochs/ tree is gone must REFUSE, never serve the root database.

Mirrors the CSO's failing suite (a592-recognition-controls-pre-patch-3d5c8ea-20260914T1104Z; C1/C4/C5/C6/C8 red
on 3d5c8ea) at unit scope. The shape rule is ONE function consumed by both resolve_db_path and open_store; a
resolver-only test would have passed on the broken build for the write path (open_store had its own check),
so both callers are exercised here.
"""
import json
from pathlib import Path

import pytest

from lbrain.epoch import (EPOCH, EPOCH_TREE_MISSING, LEGACY, MARKER_NAME, EpochError, home_shape,
                          read_marker, resolve_db_path, write_marker)


def _legacy_home(tmp_path):
    (tmp_path / "brain.db").write_bytes(b"")
    return tmp_path


def _epoch_home(tmp_path, eid="E1"):
    d = tmp_path / "epochs" / eid; d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"")
    (tmp_path / "epochs" / "CURRENT").write_text(eid + "\n")
    return tmp_path


def test_C2_legacy_never_built_is_LEGACY_and_resolves_to_the_root_db(tmp_path):
    h = _legacy_home(tmp_path)
    assert home_shape(h) == LEGACY
    assert resolve_db_path(h, h / "brain.db") == h / "brain.db"


def test_C4_marker_written_with_four_keys_and_first_epoch_preserved(tmp_path):
    write_marker(tmp_path, "E1", "0.1.x")
    m = read_marker(tmp_path)
    assert set(m) == {"first_epoch_id", "last_epoch_id", "last_published_at", "engine_version"}
    write_marker(tmp_path, "E2", "0.1.x")
    m = read_marker(tmp_path)
    assert m["first_epoch_id"] == "E1" and m["last_epoch_id"] == "E2", "first is preserved, last tracks"


def test_C1_marked_home_with_tree_gone_is_EPOCH_TREE_MISSING_and_REFUSES(tmp_path):
    h = _epoch_home(tmp_path); write_marker(h, "E1", "0.1.x")
    assert home_shape(h) == EPOCH
    import shutil; shutil.rmtree(h / "epochs")           # total tree loss; marker survives beside config
    assert home_shape(h) == EPOCH_TREE_MISSING
    with pytest.raises(EpochError) as ei:
        resolve_db_path(h, h / "brain.db")
    msg = str(ei.value).lower()
    assert "epoch" in msg and ("restore" in msg or "epoch build" in msg or "rebuild" in msg), (
        "the refusal must name the epoch and a route (CSO scored rule)")


def test_C7_deliberate_marker_removal_declares_legacy(tmp_path):
    h = _epoch_home(tmp_path); write_marker(h, "E1", "0.1.x")
    import shutil; shutil.rmtree(h / "epochs"); (h / MARKER_NAME).unlink()
    (h / "brain.db").write_bytes(b"")
    assert home_shape(h) == LEGACY
    assert resolve_db_path(h, h / "brain.db") == h / "brain.db"


def test_C3_dangling_pointer_still_refuses_as_before(tmp_path):
    h = _epoch_home(tmp_path)
    (h / "epochs" / "E1" / "brain.db").unlink()
    with pytest.raises(EpochError):
        resolve_db_path(h, h / "brain.db")


def test_C8_open_store_for_write_REFUSES_on_a_marked_treeless_home(tmp_path, monkeypatch):
    """The write path had its own shape check and returned a writable legacy Store (CSO C8)."""
    from lbrain import epoch as ep
    from lbrain.config import Config
    h = _epoch_home(tmp_path); write_marker(h, "E1", "0.1.x")
    import shutil; shutil.rmtree(h / "epochs")
    monkeypatch.setattr("lbrain.config.CONFIG_DIR", str(h))
    monkeypatch.setattr("lbrain.write_gates.check_write_target", lambda cfg, home: None)
    cfg = Config(embedding_provider="local", embedding_dim=8); cfg.db_path = h / "brain.db"
    for for_write in (False, True):
        with pytest.raises(EpochError):
            ep.open_store(cfg, for_write=for_write)
    assert not (h / "brain.db").exists() or (h / "brain.db").stat().st_size == 0, "nothing was written"


def test_countermodel_an_absent_pointer_alone_is_never_read_as_lost_history(tmp_path):
    """CCO constraint: without the marker, a treeless home is LEGACY, served, not refused."""
    h = _legacy_home(tmp_path)
    (h / "epochs").mkdir()                         # an empty tree dir and no pointer, no marker
    assert home_shape(h) == LEGACY
