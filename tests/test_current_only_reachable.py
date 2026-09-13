"""A3 re-run (CSO, 2026-09-11T07:16Z): `current_only` existed in the search layer, and no CLI or MCP
caller could pass it, so the declared exclusion was unreachable and the pass rested on the penalty
plus the serving cap. These tests pin that every caller exposes it. Behaviour under the flag is
tested where it lives: tests/test_dual_view_current_only.py and tests/test_status_retired_at_serve_time.py.
Signature checks are reachability proofs, not operational results, and are labelled as such.
"""
import inspect

import click


def test_mcp_tools_accept_current_only():
    from lbrain import mcp_server
    for fn in (mcp_server.lair_query, mcp_server.lair_search):
        p = inspect.signature(fn).parameters
        assert "current_only" in p and p["current_only"].default is False, fn.__name__


def test_cli_query_and_search_expose_the_flag():
    from lbrain.cli import query, search_cmd
    for cmd in (query, search_cmd):
        names = {o.name for o in cmd.params}
        assert "current_only" in names, cmd.name
        opt = [o for o in cmd.params if o.name == "current_only"][0]
        assert "--current-only" in opt.opts and opt.is_flag, cmd.name
        # Ask Click what the flag actually DEFAULTS TO, via the public accessor, rather than
        # reading `opt.default` — that attribute is Click's internal encoding of "unset" and it
        # changed shape: 8.3 stores False, 8.5 stores Sentinel.UNSET. Both resolve to False
        # through get_default(). Reproduced on 8.5.0 before this line was written; the old
        # assertion passed locally and failed on all four CI Pythons.
        assert opt.get_default(click.Context(cmd)) is False, cmd.name
