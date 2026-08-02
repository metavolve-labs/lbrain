"""The unsupported-interpreter path must explain itself.

Apple's /usr/bin/python3 and the python.org macOS installers are built without
--enable-loadable-sqlite-extensions. On those, sqlite3.Connection has no
enable_load_extension attribute at all and `lbrain init` — the first command in
the README — died on a bare AttributeError. These tests pin the diagnosis.
"""

from __future__ import annotations

import sqlite3

import pytest

from lbrain.store import SqliteExtensionError, Store


class _NoExtensionConnection:
    """A connection proxy missing exactly the one attribute, as CPython does."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def __getattr__(self, name: str):
        if name == "enable_load_extension":
            raise AttributeError(
                "'sqlite3.Connection' object has no attribute 'enable_load_extension'"
            )
        return getattr(self._real, name)


def test_missing_extension_support_raises_actionable_error(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: _NoExtensionConnection(real_connect(*a, **kw))
    )

    with pytest.raises(SqliteExtensionError) as excinfo:
        Store(tmp_path / "brain.db", embedding_dim=384)

    message = str(excinfo.value)
    # It must name the interpreter at fault and give a way out — a bare
    # "extension support missing" leaves the reader exactly where they started.
    assert "built without SQLite" in message
    assert "brew install python@3.12" in message
    assert "enable-loadable-sqlite-extensions" in message


def test_guard_is_a_runtime_error_subclass():
    # The MCP server and library callers construct Store directly and do not go
    # through the click layer; they must still be able to catch it broadly.
    assert issubclass(SqliteExtensionError, RuntimeError)


def test_supported_interpreter_still_opens_a_store(tmp_path):
    # Guards the guard: it must not fire on an interpreter that is fine. Skips
    # rather than fails where the test runner itself lacks support.
    if not hasattr(sqlite3.connect(":memory:"), "enable_load_extension"):
        pytest.skip("this interpreter has no loadable-extension support")
    store = Store(tmp_path / "brain.db", embedding_dim=384)
    assert store.stats()["docs"] == 0
    store.close()
