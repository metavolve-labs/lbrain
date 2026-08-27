import re

import click
from click.testing import CliRunner

from lbrain import presentation


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def render(monkeypatch, theme):
    monkeypatch.setenv("LBRAIN_THEME", theme)

    @click.command()
    def command():
        for role in ("frame", "caution", "boundary", "memory", "title"):
            presentation.echo(f"{role}: invariant", role=role)

    return CliRunner().invoke(command, color=True).output


def test_themes_change_ansi_not_content(monkeypatch):
    classic = render(monkeypatch, "classic")
    contrast = render(monkeypatch, "high-contrast")
    mono = render(monkeypatch, "mono")

    assert classic != contrast
    assert "\x1b[" in classic and "\x1b[" in contrast
    assert "\x1b[" not in mono
    assert ANSI.sub("", classic) == ANSI.sub("", contrast) == mono


def test_unknown_theme_falls_back_visibly_once(monkeypatch):
    presentation._warned_unknown.clear()
    monkeypatch.setenv("LBRAIN_THEME", "ultraviolet")

    @click.command()
    def command():
        presentation.echo("one", role="frame")
        presentation.echo("two", role="frame")

    output = CliRunner().invoke(command, color=False).output
    assert output.count("unknown LBRAIN_THEME 'ultraviolet'") == 1
    assert output.endswith("one\ntwo\n")


def test_theme_name_is_case_and_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("LBRAIN_THEME", "  HIGH-CONTRAST ")
    assert presentation.active_theme() == ("high-contrast", "")
