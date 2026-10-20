"""Every GraphQL variable fed to a TagFilter `values:` must be declared non-null.

arweave.net began rejecting `[String!]` on `values` with HTTP 400 (2026-10-20, PR #95);
this guards the next query someone adds to gcx.py.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "lbrain" / "gcx.py").read_text(encoding="utf-8")


def nullable_values_vars(src: str) -> list[str]:
    bad = []
    for var in set(re.findall(r"values:\$(\w+)", src)):
        decls = re.findall(r"\$" + var + r":(\[[A-Za-z!]+\]!?)", src)
        if not decls or any(not d.endswith("]!") for d in decls):
            bad.append(var)
    return bad


def test_gcx_passes_tag_values_through_variables():
    assert re.search(r"values:\$\w+", SRC), "expected gcx.py to pass tag values via GraphQL variables"


def test_values_variables_are_declared_non_null():
    assert nullable_values_vars(SRC) == []


def test_guard_catches_a_nullable_declaration():
    # negative control: the pre-#95 shape must be flagged
    assert nullable_values_vars('query($n:[String!]){transactions(tags:[{name:"X",values:$n}])}') == ["n"]
