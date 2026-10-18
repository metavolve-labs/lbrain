"""No-regression property for the whoami reason scrub (CSO review of #92, 2026-10-18T0227Z).

The first cut of #92 CHOSE between the raw and the percent-decoded copy by counting
[REDACTED]. That choice is not monotone: six inputs main fully redacts came out partly shown
(a decoded space splitting a value, a decoded "[REDACTED]" inflating the count, %40/%22/%0A
inside a value, a tie that left %41Iza). The fix composes instead of choosing, so anything
hidden by the raw pass stays hidden.

Two properties, over a generated corpus (every secret shape x every encoding/insertion) plus
the CSO's probe cases:
  A. NO REGRESSION: every secret fragment the frozen main scrub (tests/_scrub_reference_main.py,
     cb51589) hides stays hidden at the head;
  B. NEVER SHOWN: the shown line contains no fragment of the secret, for every case outside
     the documented accepted limits.
Synthetic key material is assembled from pieces so no literal key shape sits in the file.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

import _scrub_reference_main as main_ref
from lbrain.identity import scrub_reason

AIZA = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2"
SKT = "s" + "k-" + "abcdefghijklmnop"
GHP = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUV"
XOX = "xo" + "xb-" + "123456789012-abcdefghijkl"
AKI = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
VAL = "Zq7wX9pL2mR4"          # a secret VALUE carried by a name, not key-shaped itself

# (label, template with {S} for the secret, the secret, fragments that must stay hidden)
SHAPES = [
    ("aiza", "err {S} end", AIZA, [AIZA[-10:], AIZA[6:16]]),
    ("sk", "x {S} y", SKT, [SKT[-8:], SKT[3:11]]),
    ("ghp", "auth {S} failed", GHP, [GHP[-10:], GHP[4:14]]),
    ("xoxb", "slack {S} denied", XOX, [XOX[-10:]]),
    ("akia", "aws {S} bad", AKI, [AKI[-10:]]),
    ("env", "password={S} rejected", VAL, [VAL[-6:], VAL[2:8]]),
    ("colon", "token: {S} expired", VAL, [VAL[-6:], VAL[2:8]]),
    ("json", '{{"api_key": "{S}"}}', VAL, [VAL[-6:], VAL[2:8]]),
    ("pyrepr", "{{'api_key': '{S}'}}", VAL, [VAL[-6:], VAL[2:8]]),
    ("toml", 'GEMINI_API_KEY = "{S}"', VAL, [VAL[-6:], VAL[2:8]]),
    ("query", "GET https://h.invalid/v1?key={S}&alt=json", VAL, [VAL[-6:], VAL[2:8]]),
    ("userinfo", "connect https://admin:{S}@db.invalid/x failed", VAL, [VAL[-6:], VAL[2:8]]),
    ("bearer", "auth: Bearer {S} rejected", VAL, [VAL[-6:], VAL[2:8]]),
    ("authz", "Authorization: token {S}", VAL, [VAL[-6:], VAL[2:8]]),
    # A value that opens a container it never closes: main took it as a bare value.
    ("unclosed", "password={{{S} rejected", VAL, [VAL[-6:], VAL[2:8]]),
    ("unclosed_list", "tokens=[{S} rejected", VAL, [VAL[-6:], VAL[2:8]]),
]


def _mid(s: str, ins: str) -> str:
    h = len(s) // 2
    return s[:h] + ins + s[h:]


def _pct_all(s: str) -> str:
    return "".join(f"%{ord(c):02X}" for c in s)


# Transforms of the SECRET (name: function).
TRANSFORMS = {
    "raw": lambda s: s,
    "pct_first": lambda s: f"%{ord(s[0]):02X}" + s[1:],
    "pct_all": _pct_all,
    "pct_double_first": lambda s: f"%25{ord(s[0]):02X}" + s[1:],
    "pct_triple_first": lambda s: f"%2525{ord(s[0]):02X}" + s[1:],
    "zwsp": lambda s: _mid(s, "​"),
    "cgj": lambda s: _mid(s, "͏"),
    "vs16": lambda s: _mid(s, "️"),
    "hangul_filler": lambda s: _mid(s, "ㅤ"),
    "bom": lambda s: _mid(s, "﻿"),
    "c1_csi": lambda s: _mid(s, "\x9b"),
    "c1_nel": lambda s: _mid(s, "\x85"),
    "pct_zwsp": lambda s: _mid(s, "%E2%80%8B"),
    "pct_cgj": lambda s: _mid(s, "%CD%8F"),
    "pct_c1": lambda s: _mid(s, "%C2%9B"),
}

# Context wrappers around the whole case (the CSO's decoded-copy regressions).
WRAPPERS = {
    "bare": lambda t: t,
    "fake_placeholder": lambda t: t + " %5BREDACTED%5D%5BREDACTED%5D",
    "glued_40": lambda t: "a" * 40 + t,
    "pct_space_pw_before": lambda t: "password=p%20ss9 " + t,
    "encoded_key_after": lambda t: t + " %41" + AIZA[1:],
}

# Accepted limits (documented in the PR body): transforms whose secret may still show.
# Each is a known, listed limit; the no-regression property still holds for them.
ACCEPTED = set()


def _cases():
    for (label, tmpl, secret, frags) in SHAPES:
        for tname, tf in TRANSFORMS.items():
            for wname, wf in WRAPPERS.items():
                text = wf(tmpl.format(S=tf(secret)))
                yield (f"{label}/{tname}/{wname}", text, frags)


# The CSO's probe92 / probe92b / probe92c regression inputs, with the fragments she checked.
CSO_CASES = [
    ("pw %20 then sk-", "password=p%20ss9%20" + SKT[:11], ["ss9"]),
    ("pw %20 + fake placeholder", "password=p%20ss9 %5BREDACTED%5D", ["ss9"]),
    ("DSN pw %40 + token%3D", "postgresql://u:p%40ss9w@db.invalid/x?token%3Dt0kv9", ["ss9w", "t0kv9"]),
    ("token %22 + key%3D", "token=a%22b9c key%3Dv4lue9", ["b9c", "v4lue9"]),
    ("tie merge", "a" * 40 + "%41" + AIZA[1:] + "b" * 210 + " %41" + AIZA[1:], [AIZA[5:]]),
    ("tie url %20", "https://u:p%20ss9w@h.invalid %41" + AIZA[1:], ["ss9w", AIZA[5:]]),
    ("tie raw X decoded Y", "token=t0kval9 %41" + AIZA[1:], ["t0kval9", AIZA[1:]]),
    ("decoded %20 split + enc key", "token=abc%20def9 %41" + AIZA[1:], ["def9", AIZA[1:]]),
    ("decoded %26 in query value", "?key=a%26b9c", ["b9c"]),
    ("decoded %22 closes quote early", 'password="ab%22cd9ef" %41' + AIZA[1:], ["cd9ef", AIZA[1:]]),
    ("decoded %40 in url pw", "https://u:p%40ss9w@h.invalid %41" + AIZA[1:], ["ss9w", AIZA[1:]]),
    ("decoded %0A in bearer", "Bearer abc%0Adef9 %41" + AIZA[1:], ["def9", AIZA[1:]]),
    ("%2541Iza", "GET %2541" + AIZA[1:], [AIZA[1:]]),
    ("key%253D", "u?key%253Dv4lue9", ["v4lue9"]),
    ("%252541", "%252541" + AIZA[1:], [AIZA[1:]]),
    ("fake placeholders + raw real key", AIZA + " %5BREDACTED%5D%5BREDACTED%5D", [AIZA[4:]]),
    ("fake placeholders + raw token", "token=t0kval9xyz %5BREDACTED%5D%5BREDACTED%5D", ["t0kval9xyz"]),
    ("opaque token glued to key%3D", "x" * 41 + "%20key%3Dv4lue9 token%3Dt0kv9", ["v4lue9", "t0kv9"]),
    ("OSC8 + key%3D", "%1B]8;;http://evil%1B%5C click%1B]8;;%1B%5C key%3Dv4lue9", ["v4lue9"]),
    ("NUL/DEL/C1 + key%3D", "a%00b%7Fc%C2%85d%C2%9Be key%3Dv4lue9", ["v4lue9"]),
]

ALL = list(_cases()) + [(f"cso/{l}", t, f) for (l, t, f) in CSO_CASES]


def test_the_corpus_is_large_enough_to_mean_something():
    assert len(ALL) >= 1000, len(ALL)


@pytest.mark.parametrize("label,text,frags", ALL, ids=[c[0] for c in ALL])
def test_A_no_fragment_hidden_by_main_is_shown_by_head(label, text, frags):
    ref = main_ref.scrub_reason(text)
    head = scrub_reason(text)
    regressed = [f for f in frags if f not in ref and f in head]
    assert not regressed, (label, ref, head)


@pytest.mark.parametrize("label,text,frags", ALL, ids=[c[0] for c in ALL])
def test_B_the_shown_line_never_contains_the_secret(label, text, frags):
    if label.split("/")[1] in ACCEPTED:
        pytest.skip("documented accepted limit")
    head = scrub_reason(text)
    shown = [f for f in frags if f in head]
    assert not shown, (label, head)


@pytest.mark.parametrize("label,text,frags", ALL, ids=[c[0] for c in ALL])
def test_C_no_control_character_reaches_the_shown_line(label, text, frags):
    head = scrub_reason(text)
    bad = [hex(ord(c)) for c in head
           if ord(c) < 0x20 or ord(c) == 0x7F or 0x80 <= ord(c) < 0xA0
           or ord(c) in (0x2028, 0x2029, 0x202E, 0x200B)]
    assert not bad, (label, head)


def test_c1_controls_are_stripped():
    assert scrub_reason("a\x9bb\x85c\x80d\x9fe") == "abcde"


def test_quote_helper_round_trip_sanity():
    """Guard the generator itself: a fully percent-encoded secret really decodes to it."""
    from urllib.parse import unquote
    assert unquote(_pct_all(VAL)) == VAL and quote(VAL) == VAL
