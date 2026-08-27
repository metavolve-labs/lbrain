"""Semantic terminal styling for human-facing LBrain output.

Presentation stays downstream of retrieval and serving: a theme may change ANSI
sequences, never words, ordering, fences, trust labels, or exit status.
"""
from __future__ import annotations

import os

import click


THEMES = {
    "classic": {
        "frame": {"fg": "cyan"},
        "caution": {"fg": "yellow"},
        "boundary": {"fg": "red"},
        "memory": {"fg": "green"},
        "title": {"fg": "yellow"},
    },
    "high-contrast": {
        "frame": {"fg": "bright_blue", "bold": True},
        "caution": {"fg": "bright_yellow", "bold": True},
        "boundary": {"fg": "bright_red", "bold": True},
        "memory": {"fg": "bright_green"},
        "title": {"fg": "bright_yellow", "bold": True},
    },
    "mono": {role: {} for role in ("frame", "caution", "boundary", "memory", "title")},
}

_warned_unknown: set[str] = set()


def active_theme() -> tuple[str, str]:
    """Return ``(theme, warning)``; invalid values visibly fall back."""
    requested = os.environ.get("LBRAIN_THEME", "classic").strip().lower()
    if requested in THEMES:
        return requested, ""
    return "classic", f"[presentation] unknown LBRAIN_THEME {requested!r} — using classic."


def echo(text: str = "", *, role: str = "frame", nl: bool = True) -> None:
    """Echo themed text and report an invalid theme once per process."""
    theme, warning = active_theme()
    requested = os.environ.get("LBRAIN_THEME", "classic").strip().lower()
    if warning and requested not in _warned_unknown:
        _warned_unknown.add(requested)
        click.secho(warning, fg="yellow")
    attributes = THEMES[theme].get(role, {})
    rendered = click.style(text, **attributes) if attributes else text
    click.echo(rendered, nl=nl)
