"""Read-time identity check (CSO design 2026-11-17): three states, never a bare stored word.

Uses the real estate records beside the engine when present (the lbrain-teams checkout); otherwise the chain
cases are skipped and only the "not reachable" shape is asserted.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil

import pytest

from lbrain import chaincheck as cc

RECORDS = cc.default_records_dir()
MEDIC = "metavolvelabs/csuite/medic/sostra"


def _record(records, name):
    for p in sorted((records / "estate").glob("member-*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("member") == name:
            return p, d
    return None, None


def test_missing_records_dir_is_not_reachable(tmp_path):
    r = cc.check("gcx://metavolvelabs/csuite/cto/fable", "00" * 32, records_dir=tmp_path / "nowhere")
    assert r["state"] == cc.STATE_UNREACHABLE
    assert "not reachable" in r["detail"]


def test_empty_name_is_not_reachable():
    assert cc.check("", "")["state"] == cc.STATE_UNREACHABLE


@pytest.mark.skipif(RECORDS is None, reason="no lbrain-teams records beside the engine")
def test_real_record_verifies_now_with_its_own_key():
    p, d = _record(RECORDS, MEDIC)
    if d is None:
        pytest.skip("medic record not present")
    r = cc.check("gcx://" + MEDIC, d["member_public_key"], records_dir=RECORDS,
                 today=dt.date.fromisoformat(d["issued"]))
    assert r["state"] == cc.STATE_VERIFIED, r
    assert r["expires"] == d["expires"]


@pytest.mark.skipif(RECORDS is None, reason="no lbrain-teams records beside the engine")
def test_wrong_address_fails():
    p, d = _record(RECORDS, MEDIC)
    if d is None:
        pytest.skip("medic record not present")
    r = cc.check(MEDIC, "ab" * 32, records_dir=RECORDS)
    assert r["state"] == cc.STATE_FAILS and "claimed address" in r["detail"], r


@pytest.mark.skipif(RECORDS is None, reason="no lbrain-teams records beside the engine")
def test_tampered_record_fails(tmp_path):
    p, d = _record(RECORDS, MEDIC)
    if d is None:
        pytest.skip("medic record not present")
    fake = tmp_path / "records"
    shutil.copytree(RECORDS, fake, ignore=shutil.ignore_patterns("*.bin", "ANCHOR-*", "*bundle*"))
    tp, td = _record(fake, MEDIC)
    td["expires"] = "2099-01-01"  # a longer term than the org signed
    tp.write_text(json.dumps(td), encoding="utf-8")
    r = cc.check(MEDIC, d["member_public_key"], records_dir=fake)
    assert r["state"] == cc.STATE_FAILS and "does not verify" in r["detail"], r


@pytest.mark.skipif(RECORDS is None, reason="no lbrain-teams records beside the engine")
def test_outside_term_fails():
    p, d = _record(RECORDS, MEDIC)
    if d is None:
        pytest.skip("medic record not present")
    r = cc.check(MEDIC, d["member_public_key"], records_dir=RECORDS, today=dt.date(2099, 1, 1))
    # the chain itself has lapsed by then, so the verifier refuses before the term check does; either way FAILS
    assert r["state"] == cc.STATE_FAILS, r


@pytest.mark.skipif(RECORDS is None, reason="no lbrain-teams records beside the engine")
def test_empty_claimed_address_fails():
    """CSO 2026-11-17T11:34Z: an empty claim must not read as verified."""
    r = cc.check(MEDIC, "", records_dir=RECORDS)
    assert r["state"] == cc.STATE_FAILS and "no address claimed" in r["detail"], r


def test_pins_default_to_metavolve_when_config_is_empty():
    class C: pass
    p = cc.pins_from_config(C())
    assert p["root"] == cc.METAVOLVE_ROOT_PUBLIC_HEX and p["intermediate"] == cc.INTERMEDIATE_PUBLIC_HEX
    assert p["orgs"] == cc.ORG_PUBLIC_HEX


def test_a_home_may_pin_its_own_root_and_ours_then_fails(tmp_path):
    """2026-11-18 (C-Suite generic drive): a founder's home pins the founder's root; our records do not verify under
    it, because our root is not above theirs. The same records verify under the default pins."""
    if RECORDS is None:
        pytest.skip("no lbrain-teams records beside the engine")
    p, d = _record(RECORDS, MEDIC)
    if d is None:
        pytest.skip("medic record not present")
    class C:
        estate_root_public_hex = "11" * 32
        estate_intermediate_public_hex = "22" * 32
        estate_org_public_hex = "founderco=" + "33" * 32
    pins = cc.pins_from_config(C())
    assert pins["orgs"] == {"founderco": "33" * 32}
    r = cc.check(MEDIC, d["member_public_key"], records_dir=RECORDS, pins=pins)
    assert r["state"] == cc.STATE_FAILS, r
    r2 = cc.check(MEDIC, d["member_public_key"], records_dir=RECORDS)
    assert r2["state"] == cc.STATE_VERIFIED, r2
