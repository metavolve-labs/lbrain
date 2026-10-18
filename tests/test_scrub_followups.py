"""Follow-ups to the whoami reason scrub (CSO round 4 on #90, 2026-10-18T0149Z).

1. A nested value under a secret name leaked: in `{"credentials": {"api_key": "v"}}` the
   outer bare-value match ate the inner name, so the inner value survived.
2. Default_Ignorable_Code_Point characters that are not category Cf (U+034F, U+FE0F,
   U+3164, ...) still split a key invisibly.
Cheap extras: a URL password containing '/', a key-shaped token in the URL user field, and
percent-encoded forms (`%41Iza...`, `key%3D...`).
Synthetic key material is assembled from pieces so no literal key shape sits in the file.
"""
from __future__ import annotations

from lbrain.identity import scrub_reason

AIZA = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2"
SKT = "s" + "k-" + "abcdefghijklmnop"
GHP = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUV"


# ---------- 1. nested values ----------

def test_nested_json_under_a_secret_name():
    out = scrub_reason('{"credentials": {"api_key": "nest5dval9"}}')
    assert "nest5dval9" not in out, out


def test_nested_python_repr_under_a_secret_name():
    out = scrub_reason("{'credentials': {'api_key': 'nest6dval9'}}")
    assert "nest6dval9" not in out, out


def test_nested_value_with_a_non_secret_inner_name():
    """The whole one-level object under a secret name goes, whatever its inner names."""
    out = scrub_reason("{'credentials': {'value': 'nest4dval9'}}")
    assert "nest4dval9" not in out, out


def test_nested_list_under_a_secret_name():
    out = scrub_reason('{"tokens": ["lst1val9", "lst2val9"]}')
    assert "lst1val9" not in out and "lst2val9" not in out, out


def test_nested_object_after_an_unquoted_secret_name():
    out = scrub_reason("credentials={'value': 'nest7dval9'}")
    assert "nest7dval9" not in out, out


def test_a_deeper_container_is_never_eaten_as_a_bare_value():
    """Two levels deep the one-level container arm cannot match; the bare arm then must not
    start at `{`, or it swallows the inner `"api_key":` and leaves its value behind."""
    out = scrub_reason('{"credentials": {"api_key": "deep1val9", "opts": {}}}')
    assert "deep1val9" not in out, out


def test_control_auth_is_not_a_secret_name_but_its_inner_token_is():
    out = scrub_reason("{'auth': {'token': 'nest3dval9'}}")
    assert "nest3dval9" not in out and "'auth'" in out, out


def test_control_a_non_secret_nested_object_is_untouched():
    t = "{'options': {'timeout': 30, 'retries': 2}}"
    assert scrub_reason(t) == t


# ---------- 2. default-ignorable characters that are not Cf ----------

def test_default_ignorables_cannot_split_a_key():
    for ch in ("͏", "️", "ㅤ", "ᅟ", "ﾠ", "\U000e0100"):
        for key, cut in ((AIZA, 12), (SKT, 7), (GHP, 14)):
            text = "x " + key[:cut] + ch + key[cut:]
            out = scrub_reason(text)
            assert key[cut:] not in out and "[REDACTED]" in out, (hex(ord(ch)), out)


def test_format_chars_outside_the_ignorable_table_still_cannot_split_a_key():
    """Category Cf is stripped as a class, not only the Default_Ignorable ranges: U+FFF9 and
    U+0600 are Cf but not default-ignorable."""
    for ch in ("￹", "؀"):
        text = "x " + SKT[:7] + ch + SKT[7:]
        out = scrub_reason(text)
        assert SKT[7:] not in out, (hex(ord(ch)), out)


def test_default_ignorables_are_gone_from_ordinary_text():
    assert scrub_reason("a͏b️cㅤd") == "abcd"


def test_control_ordinary_non_ascii_text_is_kept():
    t = "no such file: /données/brain.db"
    assert scrub_reason(t) == t


# ---------- cheap extras ----------

def test_url_password_containing_a_slash():
    out = scrub_reason("https://admin:p4ss/w0rd9@db.example.invalid/x")
    assert "p4ss" not in out and "w0rd9" not in out, out
    assert "db.example.invalid/x" in out, out


def test_a_key_shaped_token_in_the_url_user_field():
    out = scrub_reason(f"GET https://{GHP}@github.com/o/r failed")
    assert GHP[4:] not in out and "github.com/o/r failed" in out, out


def test_percent_encoded_key_shape():
    out = scrub_reason("GET %41" + AIZA[1:])
    assert AIZA[4:] not in out, out


def test_percent_encoded_query_name():
    out = scrub_reason("url=https%3A%2F%2Fx%2Fv1%3Fkey%3Dpct3ncval9")
    assert "pct3ncval9" not in out, out


def test_percent_text_is_shown_decoded():
    """Compose-never-choose (CSO review of #92) always shows the decoded line: the display is
    the decoded text, redacted. Ordinary %-escapes therefore read decoded."""
    assert scrub_reason("no such file: /a%20b/brain.db") == "no such file: /a b/brain.db"


def test_yaml_quoted_password_with_spaces():
    out = scrub_reason('password: "yam1 secr3t"')
    assert "yam1" not in out and "secr3t" not in out, out
