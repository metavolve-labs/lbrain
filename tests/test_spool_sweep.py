"""Increment 2 of the capture spool: the sweep (2026-09-16).

Born of the CSO's mine 2026-09-15T18:46Z: 29 captures / 306 MB staged on one home, written by the engine and read by
nothing; four strings promised a sweep in the present tense. These tests pin the contract:

  S1  a sweep receipt beside an entry removes it from staged_items() and staged_count(); include_swept restores it
  S2  swept_items() lists exactly the receipted entries; a torn entry is never listed either way
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


def test_s2_swept_items_lists_exactly_the_receipted_and_never_a_torn_entry(tmp_path):
    home = tmp_path / "home"; metas = _stage(home, 3)
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "cd" * 16})
    # torn: receipt + meta but the payload is gone
    write_sweep_receipt(metas[2], {"epoch_id": "E1", "txid": "ef" * 16}); (metas[2].with_name(metas[2].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX)).unlink()
    assert swept_items(home) == [metas[0]]
    assert staged_items(home) == [metas[1]]


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
            return f"· already captured: t1 txid {'cd' * 32}"   # P-A: the full txid, as the fixed CLI prints it
        if i == 3:
            raise eb.EpochError("`lbrain archive capture` failed in staging (rc 2):\n✗ boom")
        (staging_home / "archive").mkdir(exist_ok=True); (staging_home / "archive" / ("aa" * 32 + ".bin")).write_bytes(b"ct")
        return f"✓ Captured 't0' → local\n  txid {'aa' * 32}  ·  13 bytes  ·  snapshot 5 chars indexed\n"
    monkeypatch.setattr(eb, "_run_cli", fake_run)
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    assert [a[:1] for a, _ in seen] == [["capture"]] * 3 and all(sh == staging for _, sh in seen)
    assert "--from-file" in seen[0][0] and "--session-id" in seen[0][0] and "sid-0" in seen[0][0]
    sw = report["sweep"]
    assert (sw["staged"], sw["swept"], sw["already"], sw["failed"]) == (3, 1, 1, 1) and sw["failures"][0].startswith(metas[2].name)
    assert len(plan) == 2 and plan[0]["txid"] == "aa" * 32 and plan[1]["already"] is True and plan[1]["txid"] == "cd" * 32
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


def test_s7_keyless_receipt_is_healed_and_recaptured(tmp_path, monkeypatch):
    import sqlite3
    from lbrain.spool import keyless_receipts
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 2)
    (home / "keys").mkdir(); (home / "keys" / ("ok" * 16 + ".key")).write_bytes(b"k")
    write_sweep_receipt(metas[0], {"epoch_id": "E1", "txid": "ok" * 16})       # genuine: key present
    write_sweep_receipt(metas[1], {"epoch_id": "E1", "txid": "bad" * 10})      # false: no key
    assert [t for _, t in keyless_receipts(home)] == ["bad" * 10]
    # a candidate db with the keyless row present
    con = eb._connect_vec(staging / "brain.db"); con.row_factory = sqlite3.Row
    from lbrain.archive.storage import ArchiveStore
    st = ArchiveStore(con, 4); st.ensure_schema()
    st.insert_archive(txid="bad" * 10, namespace="private", title="t1", snapshot="snap", tags={}, n_bytes=13, created=0.0, transport="local", source_hash="h")
    con.commit(); con.close()
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    seen = []
    def fake_run(args, staging_home, lbrain_bin, lock=None, **kw):
        seen.append(args); return f"✓ Captured 't1' → local\n  txid {'new' * 10}  ·  13 bytes\n"
    monkeypatch.setattr(eb, "_run_cli", fake_run)
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    assert report["sweep"]["healed_keyless"] == 1 and report["sweep"]["staged"] == 1 and len(seen) == 1   # only the false one re-staged
    assert not sweep_receipt_path(metas[1]).is_file() and sweep_receipt_path(metas[0]).is_file()
    con = eb._connect_vec(staging / "brain.db")
    assert con.execute("SELECT shredded FROM archives WHERE txid=?", ("bad" * 10,)).fetchone()[0] == 1
    con.close()
    assert plan[0]["txid"] == "new" * 10


def test_s8_torn_meta_is_counted_and_named_and_the_sweep_goes_on(tmp_path, monkeypatch):
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 2)
    metas[0].write_text("{not json")   # torn sidecar (P-B)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    monkeypatch.setattr(eb, "_run_cli", lambda args, sh, lb, lock=None, **kw: f"✓ Captured\n  txid {'ab' * 32}  ·  13 bytes\n")
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    sw = report["sweep"]
    assert sw["failed"] == 1 and metas[0].name in sw["failures"][0] and "JSONDecodeError" in sw["failures"][0]
    assert sw["swept"] == 1 and len(plan) == 1   # the other entry was swept; nothing aborted


def test_s9_refusal_forensics_never_retain_keys(tmp_path):
    staging = tmp_path / "staging"; (staging / "archive").mkdir(parents=True); (staging / "keys").mkdir()
    (staging / "archive" / "x.bin").write_bytes(b"ct"); (staging / "keys" / "x.key").write_bytes(b"wrapped"); (staging / "brain.db").write_bytes(b"db")
    fdir = tmp_path / "E1.failed"
    eb._retain_failed(staging, fdir)
    assert (fdir / "archive" / "x.bin").is_file() and (fdir / "brain.db").is_file()
    assert not (fdir / "keys").exists()   # P-C


def test_s10_reclaim_only_after_a_verified_round_trip(tmp_path, monkeypatch):
    from lbrain.archive import crypto
    from lbrain.archive.archiver import Keystore, LocalTransport
    home = tmp_path / "home"; metas = _stage(home, 2)
    payload0 = metas[0].with_name(metas[0].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX); payload1 = metas[1].with_name(metas[1].name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX)
    import hashlib
    for m, p in ((metas[0], payload0), (metas[1], payload1)):
        d = json.loads(m.read_text()); d["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest(); m.write_text(json.dumps(d))
    tr = LocalTransport(home / "archive"); ks = Keystore(home / "keys")
    env0, key0 = crypto.encrypt(payload0.read_bytes(), "pw"); tx0 = tr.put(env0, {}); ks.put(tx0, key0)
    env1, key1 = crypto.encrypt(b"DIFFERENT BYTES", "pw"); tx1 = tr.put(env1, {}); ks.put(tx1, key1)   # archived bytes != spooled bytes
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    report = {"sweep": {}}
    plan = [{"meta": str(metas[0]), "sha256": json.loads(metas[0].read_text())["sha256"], "txid": tx0},
            {"meta": str(metas[1]), "sha256": json.loads(metas[1].read_text())["sha256"], "txid": tx1}]
    n = eb._reclaim_verified(home, plan, report)
    assert n == 1 and not payload0.exists() and payload1.exists()   # P-D: drained only where the round trip matched
    assert report["sweep"]["reclaim_mismatch"] == [tx1]
    assert metas[0].is_file()   # meta stays as the record


def test_s11_full_build_carries_the_archive_index(tmp_path):
    import sqlite3
    prior = tmp_path / "prior.db"; cand = tmp_path / "cand.db"
    con = eb._connect_vec(prior); con.row_factory = sqlite3.Row
    from lbrain.archive.storage import ArchiveStore
    st = ArchiveStore(con, 4); st.ensure_schema()
    st.insert_archive(txid="T1", namespace="private", title="one", snapshot="## user\nhello", tags={}, n_bytes=5, created=1.0, transport="local", source_hash="h1")
    st.write_archive_embedding("T1", b"\x00\x00\x80\x3f" * 4)
    con.commit(); con.close()
    c2 = eb._connect_vec(cand); c2.close()
    n = eb._carry_archive_index(prior, cand)   # P-F
    assert n == 1
    c3 = eb._connect_vec(cand); c3.row_factory = sqlite3.Row
    assert c3.execute("SELECT txid, embedded FROM archives").fetchone()["txid"] == "T1"
    assert c3.execute("SELECT count(*) FROM fts_archives WHERE txid='T1'").fetchone()[0] == 1
    assert c3.execute("SELECT count(*) FROM vec_archives").fetchone()[0] == 1
    c3.close()


def test_s12_already_captured_must_print_the_full_txid(tmp_path, monkeypatch):
    home = tmp_path / "home"; staging = tmp_path / "staging"; staging.mkdir(); metas = _stage(home, 2)
    import lbrain.archive.cli as acli
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "pw")
    outs = ["· already captured: t0 (0123456789abcdef…)",            # the OLD prefix form (P-A): must be a FAILED entry, never a receipt
            f"· already captured: t1 txid {'cd' * 32}"]              # the fixed form: full txid
    monkeypatch.setattr(eb, "_run_cli", lambda args, sh, lb, lock=None, **kw: outs.pop(0))
    report: dict = {}
    plan = eb._sweep_spool(home, staging, "lbrain", None, report)
    sw = report["sweep"]
    assert sw["failed"] == 1 and "no full txid" in sw["failures"][0] and metas[0].name in sw["failures"][0]
    assert sw["already"] == 1 and plan[0]["txid"] == "cd" * 32 and plan[0]["already"] is True
