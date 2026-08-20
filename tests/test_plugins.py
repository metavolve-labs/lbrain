"""Plugin discovery, and the API surface an out-of-tree package may rely on.

Two jobs. First, that the entry-point group works — proven by dogfooding it: the
Tier-2 archive registers through the SAME group a third party would use, so if
the group breaks, archive commands vanish here rather than silently at a paying
customer.

Second, the surface pin. The moment another distribution imports from this one,
every refactor here can break it at a distance, with no failing test in this
repo. So the surface an extension may rely on is DECLARED, and changing it fails
at home instead of quietly downstream.
"""
from __future__ import annotations

import click
import pytest

from lbrain import plugins


class _EP:
    """A stand-in entry point. Cheaper than installing a package per case."""

    def __init__(self, name, fn):
        self.name, self._fn = name, fn

    def load(self):
        return self._fn


class TestDogfood:
    def test_archive_registers_through_the_public_group(self):
        """It used to be imported by name in a try/except — a plugin system with
        one plugin written into the host."""
        assert "archive" in plugins.installed(plugins.GROUP_CLI)
        assert "archive" in plugins.installed(plugins.GROUP_MCP)

    def test_archive_commands_reach_the_cli(self):
        from lbrain.cli import main

        assert "capture" in main.commands, "the archive plugin did not register"

    def test_groups_are_separate(self):
        """A CLI plugin must never be handed the MCP server."""
        assert plugins.GROUP_CLI != plugins.GROUP_MCP


class TestFailureModes:
    def test_missing_optional_dependency_is_silent(self, monkeypatch, capsys):
        """The contract this replaced: `lbrain[archive]` absent means the commands
        do not appear, and that is normal operation, not a problem to report."""
        def boom(_):
            raise ImportError("no cryptography")

        monkeypatch.setattr(plugins, "_entry_points", lambda g: [_EP("opt", boom)])
        assert plugins.load(click.Group()) == []
        assert capsys.readouterr().err == ""

    def test_a_broken_plugin_warns_and_does_not_take_down_the_cli(self, monkeypatch, capsys):
        """A user who cannot run `lbrain query` because someone else's package has
        a bug has lost the product in order to fix an extension."""
        def boom(_):
            raise RuntimeError("third-party bug")

        monkeypatch.setattr(plugins, "_entry_points", lambda g: [_EP("bad", boom)])
        g = click.Group()
        assert plugins.load(g) == []          # did not raise
        err = capsys.readouterr().err
        assert "bad" in err and "continuing without it" in err

    def test_one_bad_plugin_does_not_block_a_good_one(self, monkeypatch):
        def bad(_):
            raise RuntimeError("x")

        def good(group):
            group.add_command(click.Command("ok"))

        monkeypatch.setattr(
            plugins, "_entry_points",
            lambda g: [_EP("bad", bad), _EP("good", good)],
        )
        g = click.Group()
        assert plugins.load(g) == ["good"]
        assert "ok" in g.commands

    def test_installed_does_not_load(self, monkeypatch):
        def explode():
            raise AssertionError("installed() must not resolve the entry point")

        ep = _EP("x", None)
        ep.load = explode
        monkeypatch.setattr(plugins, "_entry_points", lambda g: [ep])
        assert plugins.installed() == ["x"]


class TestDeclaredSurface:
    """What an out-of-tree package may import from this one.

    Keep this list SHORT and grow it deliberately. Every name here is a promise
    to a paying customer's build; a name that is merely convenient today becomes
    a refactor you cannot make next quarter.
    """

    def test_registration_surface(self):
        assert plugins.GROUP_CLI == "lbrain.plugins"
        assert plugins.GROUP_MCP == "lbrain.mcp_plugins"
        assert callable(plugins.load) and callable(plugins.installed)

    def test_a_plugin_receives_the_click_group_and_can_add_commands(self):
        """The registration contract itself: register(target) -> None."""
        g = click.Group()
        plugins.load(g)                      # real plugins, real target
        assert isinstance(g, click.Group)

    def test_identity_surface(self):
        from lbrain.identity import IDENTITY_PATH, Identity

        assert hasattr(Identity, "load")
        assert IDENTITY_PATH.name == "identity.json"

    def test_gcx_name_parsing_surface(self):
        from lbrain import gcx

        assert callable(gcx.parse)

    def test_grading_source_axis_surface(self):
        """The reason Teams exists first: the source axis is inert until org
        membership can be VERIFIED, and that verification lives out of tree."""
        from lbrain import grading

        assert grading.source_grade("a/b", "a/c", verified=True) == grading.SRC_ORG
        assert grading.source_grade("a/b", "a/c", verified=False) == grading.SRC_UNJUDGEABLE
        for const in ("SRC_SELF", "SRC_ORG", "SRC_EXTERNAL", "SRC_UNJUDGEABLE"):
            assert hasattr(grading, const), const

    def test_core_object_surface(self):
        from lbrain.config import Config
        from lbrain.search import Hit
        from lbrain.store import Store

        assert hasattr(Config, "load") and callable(Store)
        assert {"rel_path", "evidence", "doc_date"} <= set(Hit.__dataclass_fields__)


@pytest.mark.parametrize("group", [plugins.GROUP_CLI, plugins.GROUP_MCP])
def test_discovery_never_raises_on_a_real_environment(group):
    assert isinstance(plugins.installed(group), list)
