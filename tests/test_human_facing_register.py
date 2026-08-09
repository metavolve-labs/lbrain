"""Human-facing surfaces state the product. They do not apologize for it.

Why this exists as a TEST and not a style note: we already had the rule in prose
("never claim peer review") and it produced the opposite failure — the safe move
became saying the worst true thing, every time, on the front door. A prose rule
is a passive binding. This one runs.

**The distinction it enforces.** Accuracy is mandatory; confession is not. Never
claiming something false is the rule. Volunteering the absence of a thing nobody
asked about is a different thing wearing the same costume, and it reads as guilt.
"Preprint with a DOI" already tells a reader it is not peer-reviewed; appending
"not peer-reviewed, and not under review at any venue" adds no accuracy and costs
credibility.

**Deliberately narrow.** This is the opposite trade from `scan-secrets.sh`, where
a false positive costs a glance and a false negative costs a credential. Here a
false positive costs trust in the check itself, and a linter people disable
protects nothing. So: a short list of phrases that are never right on a front
door, plus an allowlist where a genuine exception must carry a written reason.

**Not covered, on purpose.** Internal design docs, the claims ledger and anomaly
registers SHOULD be brutally self-critical — that is their job. Only surfaces a
prospective user reads cold are in scope.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Surfaces a person reads before they trust us. Start narrow; add deliberately.
HUMAN_FACING = ["README.md"]

# Phrases that are never right on a front door. Each one either invites the
# reader to distrust the product or apologizes for its existence.
NEVER = {
    r"at your own risk": "invites the reader to distrust the product in its first paragraph",
    r"we claim no\b": "disclaims before stating; say what IS ours",
    r"we make no claim": "same shape as 'we claim no'",
    r"still being hardened": "tells a prospective user the product is not ready for them",
    r"not yet an audited guarantee": "legal hedging on a page that is not a contract",
    r"can(?:'|no)t afford to have (?:it )?read": "implies the tool leaks; fatal for a local-first product",
    r"\bunfortunately\b": "narrator apologizing",
    r"\badmittedly\b": "narrator apologizing",
    r"we (?:apologi[sz]e|are sorry)": "no",
    r"to be fair to (?:us|ourselves)": "defensive framing",
    r"we should (?:probably|admit)": "hedged self-deprecation",
}

# Volunteered negatives. Sometimes genuinely needed — "no independent security
# audit yet" on a beta is honest and useful. So these are not banned; they must
# be DECLARED, with a reason a future reader can check. Unlisted use fails.
VOLUNTEERED_NEGATIVE = [
    r"not peer[- ]reviewed",
    r"not under review",
    r"no independent (?:security )?(?:audit|review)",
    r"has not (?:yet )?been (?:through|audited)",
]

ALLOWED: dict[str, str] = {
    "no independent security audit yet":
        "Beta notice. A prospective user genuinely needs this to size their own risk, "
        "and it is one clause rather than a paragraph of hedging.",
}


def _surfaces():
    for rel in HUMAN_FACING:
        p = ROOT / rel
        if p.exists():
            yield rel, p.read_text(encoding="utf-8")


def test_no_apologetic_register_on_human_facing_surfaces():
    offenders = []
    for rel, text in _surfaces():
        low = text.lower()
        for pat, why in NEVER.items():
            for m in re.finditer(pat, low):
                line = low[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}  /{pat}/ — {why}")
    assert not offenders, (
        "apologetic register on a human-facing surface:\n  " + "\n  ".join(offenders)
        + "\n\nAccuracy is mandatory; confession is not. State the product."
    )


def test_volunteered_negatives_are_declared_with_a_reason():
    """A negative may stay — but somebody has to have decided it should."""
    undeclared = []
    for rel, text in _surfaces():
        low = text.lower()
        for pat in VOLUNTEERED_NEGATIVE:
            for m in re.finditer(pat, low):
                window = low[max(0, m.start() - 90): m.end() + 90]
                if any(k.lower() in window for k in ALLOWED):
                    continue
                line = low[: m.start()].count("\n") + 1
                undeclared.append(f"{rel}:{line}  {low[m.start():m.end()]!r}")
    assert not undeclared, (
        "undeclared volunteered negative(s):\n  " + "\n  ".join(undeclared)
        + "\n\nEither cut it, or add it to ALLOWED with the reason it earns its place. "
        "The test is: would omitting it MISLEAD a reader? If not, omit it."
    )


@pytest.mark.parametrize("bad,pat", [
    ("Use at your own risk.", r"at your own risk"),
    ("We claim no priority over any of this.", r"we claim no\b"),
    ("a tool that is still being hardened", r"still being hardened"),
])
def test_the_check_actually_catches_these(bad, pat):
    """Guard against the guard: a linter whose patterns never match is inert —
    the A-532 / A-438 failure mode, applied to prose."""
    assert re.search(pat, bad.lower()), f"pattern /{pat}/ no longer matches its own example"
