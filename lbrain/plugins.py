"""Plugin discovery — how an out-of-tree package extends this one.

The engine already had this shape twice, hardcoded: `cli.py` and `mcp_server.py`
each imported the optional Tier-2 archive subpackage inside a `try/except
ImportError` and called its `register()`. Core ran byte-identical when the
optional dependency was absent. That is a plugin system with exactly one plugin
name written into the host.

This generalises it to the standard Python entry-point group so a package that
lives in ANOTHER distribution can register commands and tools without this
repository knowing it exists:

    [project.entry-points."lbrain.plugins"]
    myext = "myext.register:register_cli"

Each entry point resolves to a callable taking the registration target — the
click group for CLI plugins, the MCP server for tool plugins — and attaching to
it. Groups are separate so a CLI plugin is never handed an MCP server.

Two failure modes, deliberately treated differently:

  * **ImportError → silent.** That is the optional-dependency contract this
    replaces: `lbrain[archive]` not installed means the archive commands do not
    appear, and that is normal operation, not a problem to report.
  * **Anything else → warn, and carry on.** A third-party plugin that raises on
    registration must not take down the core CLI. A user who cannot run
    `lbrain query` because someone else's package has a bug has lost the product
    to fix an extension.

SUPPLY CHAIN: entry points mean any installed distribution can register commands.
That is the standard Python plugin model and the same trust boundary as
installing the package at all, but it is worth naming — this is why a MODULE
(lbrain/modules) may never ship executable content: content and code arrive
through different doors on purpose, and only one of them is auditable by reading
the markdown.
"""
from __future__ import annotations

import sys

GROUP_CLI = "lbrain.plugins"
GROUP_MCP = "lbrain.mcp_plugins"


def _entry_points(group: str):
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=group))          # 3.10+
    except TypeError:                                    # pragma: no cover
        return list(entry_points().get(group, []))


def load(target, group: str = GROUP_CLI) -> list[str]:
    """Register every discoverable plugin onto ``target``. Returns names loaded.

    Never raises. See the module docstring for why a broken plugin warns instead.
    """
    loaded: list[str] = []
    for ep in _entry_points(group):
        try:
            ep.load()(target)
        except ImportError:
            continue                                     # optional dep absent
        except Exception as e:                           # noqa: BLE001
            print(
                f"[lbrain] WARNING: plugin {ep.name!r} failed to register ({e}); "
                "continuing without it.",
                file=sys.stderr,
            )
            continue
        loaded.append(ep.name)
    return loaded


def installed(group: str = GROUP_CLI) -> list[str]:
    """Names of discoverable plugins, without loading them. For `lbrain doctor`."""
    return sorted(ep.name for ep in _entry_points(group))
