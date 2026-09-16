"""Increment 2 of the capture spool: the sweep (2026-09-16).

Born of the CSO's mine 2026-09-15T18:46Z: 29 captures / 306 MB staged on one home, written by the engine and read by
nothing; four strings promised a sweep in the present tense. These tests pin the contract:

  S1  a sweep receipt beside an entry removes it from staged_items() and staged_count(); include_swept restores it
  S2  swept_items() lists every receipted entry, payload present or drained (X1); a torn entry (no payload, no receipt) is nowhere
  S3  _sweep_spool: no passphrase -> nothing runs, report says why, plan empty, entries stay staged (loud skip)
  S4  _sweep_spool: with a passphrase, one `archive capture` per entry runs IN THE STAGING HOME, the txid is parsed,
      "already captured" is counted separately, a failing capture is counted and named, and NO receipt exists yet
  S5  _finish_sweep: ciphertext files from staging/archive land in home/archive (existing files untouched), one receipt
      per entry with epoch id + txid + bytes; afterwards staged_count() is 0 and swept_items() is complete
  S6  build(..., sweep=False) never touches the spool (the --no-sweep path)
  S7  (2b) a receipt whose txid has no local key is FALSE: keyless_receipts() lists it; the sweep removes the receipt,
      marks the row shredded in the CANDIDATE db, and re-captures the payload (found by the CTO 2026-09-16: the first
      live sweep left all 20 wrapped keys in the staging home -- the control had checked the listing, not a retrieve)
Nothing here encrypts or embeds: the CLI runner is faked, so the contract is the sweep's, not the archiver's.
"""
import json
from pathlib import Path

import pytest

from lbrain import epoch_build as eb
from lbrain.spool import (META_SUFFIX, PAYLOAD_SUFFIX, staged_count, staged_items, staging_dir, swept_items,
                          sweep_receipt_path, write_sweep_receipt)


def _stage(home: Path, n: int) -> list[Path]:
    d = staging_dir(home); d.mkdir(parents=True, exist_ok=True)
    metas = []
    for i in range(n):
        stem = f"{i:032x}"
        (d / f"{stem}{PAYLOAD_SUFFIX}").write_bytes(b"transcript %d\n" % i)
        m = {"schema": 1, "sha256": stem * 2, "size": 13, "session_id": f"sid-{i}", "title": f"t{i}", "namespace": None,
             "captured_at": "2026-09-15T22:00:00Z"}
        p = d / f"{stem}{META_SUFFIX}"; p.write_text(json.dumps(m)); metas.append(p)
    return metas


def test_s1_receipt_hides_entry_from_staged_counts(tmp_path):
    home = tmp_path / "home"; metas = _stage(home, 3)
    assert staged_count(home) == 3
    write_sweep_receipt(metas[1], {"epoch_id": "E1", "txid": "ab" * 16})
    assert staged_count(home) == 2 and metas[1] not in staged_items(home)
    assert metas[1] in staged_items(home, include_swept=True)
    assert sweep_receipt_path(metas[1]).is_file() and json.loads(sweep_receipt_path(metas[1]).read_text())["epoch_id"] == "E1"


def test_s2_swept_items_lists_exactly_the_receipted_and_a_torn_entry_is_nowhere(tmp_path):
    """A receipt is the completeness marker of a swept record: with or without its payload (drained, X1) it is swept.
    A torn entry is meta WITHOUT payload and WITHOUT receipt: it is neither staged nor swept."""
    home = tmp_path / "home"; metas = _stage(home, 3)
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "C" * 43})
    write_sweep_receipt(metas[2], {"epoch_id": "E1", "txid": "E" * 43}); (metas[2].with_name(metas[2].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX)).unlink()   # drained
    torn = _stage(home, 4)[3]; torn.with_name(torn.name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX).unlink()   # torn: no payload, no receipt
    assert swept_items(home) == [metas[0], metas[2]]
    assert staged_items(home) == [metas[1]] and torn not in staged_items(home, include_swept=True)


def test_s3_no_passphrase_is_a_loud_skip_and_touches_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); _stage(home, 2)
    monkeypatch.delenv("LBRAIN_ARCHIVE_PASSPHRASE", raising=False)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: None)
    calls = []
    monkeypatch.setattr(eb, "_run_cli", lambda args, sh, lb, lock=None, **kw: calls.append(args) or "")
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    assert plan == [] and calls == []
    assert report["sweep"]["staged"] == 2 and "passphrase" in report["sweep"]["skipped"]
    assert staged_count(home) == 2 and swept_items(home) == []


def test_s4_sweep_runs_one_capture_per_entry_in_staging_and_writes_no_receipt_yet(tmp_path, monkeypatch):
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 3)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    seen = []
    def fake_run(args, staging_home, lbrain_bin, lock=None, **kw):
        seen.append((args, staging_home))
        i = len(seen)
        if i == 2:
            return f"· already captured: t1 ({'B' * 16}…)\nLBRAIN-TXID {'B' * 43}\n"   # P-A: the machine line, as the fixed CLI prints it
        if i == 3:
            raise eb.EpochError("`lbrain archive capture` failed in staging (rc 2):\n✗ boom")
        (staging_home / "archive").mkdir(exist_ok=True); (staging_home / "archive" / ("A" * 43 + ".bin")).write_bytes(b"ct")
        return f"✓ Captured 't0' → local\n  txid {'A' * 43}  ·  13 bytes  ·  snapshot 5 chars indexed\nLBRAIN-TXID {'A' * 43}\n"
    monkeypatch.setattr(eb, "_run_cli", fake_run)
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    assert [a[:1] for a, _ in seen] == [["capture"]] * 3 and all(sh == staging for _, sh in seen)
    assert "--from-file" in seen[0][0] and "--session-id" in seen[0][0] and "sid-0" in seen[0][0]
    sw = report["sweep"]
    assert (sw["staged"], sw["swept"], sw["already"], sw["failed"]) == (3, 1, 1, 1) and sw["failures"][0].startswith(metas[2].name)
    assert len(plan) == 2 and plan[0]["txid"] == "A" * 43 and plan[1]["already"] is True and plan[1]["txid"] == "B" * 43
    assert staged_count(home) == 3 and swept_items(home) == []   # receipts only after publish


def test_s5_finish_sweep_copies_ciphertext_and_writes_receipts(tmp_path):
    home = tmp_path / "home"; staging = tmp_path / "staging"; metas = _stage(home, 2)
    (staging / "archive").mkdir(parents=True); (staging / "archive" / "new.bin").write_bytes(b"new"); (staging / "archive" / "new.tags.json").write_text("{}")
    (home / "archive").mkdir(); (home / "archive" / "old.bin").write_bytes(b"old"); (staging / "archive" / "old.bin").write_bytes(b"DIFFERENT")
    plan = [{"meta": str(metas[0]), "sha256": "x", "bytes": 13, "txid": "ab" * 32, "already": False, "captured_at": None},
            {"meta": str(metas[1]), "sha256": "y", "bytes": 13, "txid": "cd" * 32, "already": True, "captured_at": None}]
    (staging / "keys").mkdir(); (staging / "keys" / ("ab" * 32 + ".key")).write_bytes(b"wrapped-dek")   # only the FIRST record's key exists
    report = {"sweep": {}}
    eb._finish_sweep(home, staging, "E9", plan, report)
    assert (home / "archive" / "new.bin").read_bytes() == b"new" and (home / "archive" / "new.tags.json").is_file()
    assert (home / "archive" / "old.bin").read_bytes() == b"old"   # existing ciphertext is never overwritten
    assert (home / "keys" / ("ab" * 32 + ".key")).read_bytes() == b"wrapped-dek"   # 2b: the wrapped key travels with the ciphertext
    assert report["sweep"]["ciphertext_files_copied"] == 2 and report["sweep"]["key_files_copied"] == 1
    assert report["sweep"]["receipts"] == 1 and report["sweep"]["no_receipt_key_missing"] == ["cd" * 32]   # 2b: no key, no receipt
    assert staged_count(home) == 1 and swept_items(home) == [metas[0]]
    r = json.loads(sweep_receipt_path(metas[0]).read_text())
    assert r["epoch_id"] == "E9" and r["txid"] == "ab" * 32 and r["already_archived"] is False and r["bytes"] == 13


def test_s6_build_signature_carries_the_no_sweep_switch():
    import inspect
    assert inspect.signature(eb.build).parameters["sweep"].default is True


def test_s7_keyless_receipt_is_healed_and_recaptured_on_a_table_less_candidate(tmp_path, monkeypatch):
    """X5: the heal must run on a --full candidate that has NO archive tables yet (the exact shape 2b was written for)."""
    import sqlite3
    from lbrain.spool import keyless_receipts
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 2)
    (home / "keys").mkdir(); (home / "keys" / ("K" * 43 + ".key")).write_bytes(b"k")
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "K" * 43})       # genuine: key present
    write_sweep_receipt(metas[1], {"epoch_id": "E1", "txid": "X" * 43})       # false: no key
    assert [t for _, t in keyless_receipts(home)] == ["X" * 43]
    eb._connect_vec(staging / "brain.db").close()   # a bare candidate: NO archive tables (the --full shape)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    seen = []
    def fake_run(args, staging_home, lbrain_bin, lock=None, **kw):
        seen.append(args); return f"✓ Captured 't1' → local\nLBRAIN-TXID {'N' * 43}\n"
    monkeypatch.setattr(eb, "_run_cli", fake_run)
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report, dim=4)
    assert report["sweep"]["healed_keyless"] == 1 and report["sweep"]["staged"] == 1 and len(seen) == 1   # only the false one re-staged
    assert not sweep_receipt_path(metas[1]).is_file() and sweep_receipt_path(metas[0]).is_file()
    con = eb._connect_vec(staging / "brain.db")
    assert con.execute("SELECT sql FROM sqlite_master WHERE name='vec_archives'").fetchone()[0].find("float[4]") > 0   # created at the REAL width
    con.close()
    assert plan[0]["txid"] == "N" * 43


def test_s7b_heal_failure_is_loud_never_a_normal_report(tmp_path, monkeypatch):
    """X5: a heal that cannot run must abort the build, not file an error and report staged: 0."""
    import pytest
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 1)
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "X" * 43})   # false receipt, no key
    eb._connect_vec(staging / "brain.db").close()
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    monkeypatch.setattr(eb, "_run_cli", lambda *a, **k: "")
    report: dict = {}
    with pytest.raises(eb.EpochError) as ei:
        eb._sweep_spool(home, staging, "lbrain", None, report, dim=0)   # width 0 cannot create a vec table
    assert "heal FAILED" in str(ei.value) and report["sweep_heal_errors"] and sweep_receipt_path(metas[0]).is_file()   # the false receipt is left for the operator to see, the build stopped


def test_x1_drained_entries_stay_visible_to_heal_and_status(tmp_path):
    from lbrain.spool import keyless_receipts, receipted_items
    home = tmp_path / "home"; metas = _stage(home, 2)
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "X" * 43})
    metas[0].with_name(metas[0].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX).unlink()   # drained
    assert receipted_items(home) == [metas[0]] and swept_items(home) == [metas[0]]
    assert [t for _, t in keyless_receipts(home)] == ["X" * 43]   # the false receipt on a drained entry is still found
    assert staged_items(home) == [metas[1]] and staged_count(home) == 1


def test_s10b_no_key_means_no_reclaim(tmp_path, monkeypatch):
    """M12: the spool never drains a record whose wrapped key is missing."""
    home = tmp_path / "home"; metas = _stage(home, 1)
    payload = metas[0].with_name(metas[0].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX)
    (home / "archive").mkdir(); (home / "archive" / ("Z" * 43 + ".bin")).write_bytes(b"ct")
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    report = {"sweep": {}}
    n = eb._reclaim_verified(home, [{"meta": str(metas[0]), "sha256": "x", "txid": "Z" * 43}], report)
    assert n == 0 and payload.exists()


def test_s11b_carry_fails_closed_on_lost_vectors_and_uses_the_priors_width(tmp_path):
    """X2/X3/X4: an embedded row without its vector aborts the carry; the candidate's vec width comes from the prior's DDL."""
    import pytest, sqlite3
    prior = tmp_path / "prior.db"; cand = tmp_path / "cand.db"
    con = eb._connect_vec(prior); con.row_factory = sqlite3.Row
    from lbrain.archive.storage import ArchiveStore
    st = ArchiveStore(con, 8); st.ensure_schema()
    st.insert_archive(txid="T1", namespace="private", title="one", snapshot="## user\nhello", tags={}, n_bytes=5, created=1.0, transport="local", source_hash="h1")
    st.write_archive_embedding("T1", b"\x00\x00\x80\x3f" * 8)
    con.execute("DELETE FROM vec_archives")   # the vector is lost but the row says embedded=1
    con.commit(); con.close()
    eb._connect_vec(cand).close()
    with pytest.raises(eb.EpochError) as ei:
        eb._carry_archive_index(prior, cand)
    assert "no vector" in str(ei.value)
    # restore the vector: the carry succeeds and the candidate's width is the prior's (8), not a default
    con = eb._connect_vec(prior); con.row_factory = sqlite3.Row
    ArchiveStore(con, 8).write_archive_embedding("T1", b"\x00\x00\x80\x3f" * 8); con.commit(); con.close()
    assert eb._carry_archive_index(prior, cand) == 1
    c3 = eb._connect_vec(cand)
    assert "float[8]" in c3.execute("SELECT sql FROM sqlite_master WHERE name='vec_archives'").fetchone()[0]
    assert c3.execute("SELECT count(*) FROM vec_archives").fetchone()[0] == 1
    c3.close()


def test_s12_txid_comes_only_from_the_machine_line_never_from_a_title(tmp_path, monkeypatch):
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 3)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    hostile = "txid " + "H" * 43
    outs = ["· already captured: t0 (0123456789abcdef…)",                                  # old prefix form: no machine line -> FAILED
            f"✓ Captured '{hostile}' → local\n  txid {hostile[5:]}  ·  13 bytes\nLBRAIN-TXID {'R' * 43}\n",   # X7: a title that imitates the prose; the machine line wins
            f"LBRAIN-TXID {'tooshort'}\n"]                                                 # M2: a token that is not a 43-char base64url id -> FAILED
    monkeypatch.setattr(eb, "_run_cli", lambda args, sh, lb, lock=None, **kw: outs.pop(0))
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    sw = report["sweep"]
    assert sw["failed"] == 2 and all("no LBRAIN-TXID line" in f for f in sw["failures"])
    assert sw["swept"] == 1 and plan[0]["txid"] == "R" * 43
