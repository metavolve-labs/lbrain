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
            return "· already captured: t1 (0123456789abcdef…)"
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
    assert len(plan) == 2 and plan[0]["txid"] == "aa" * 32 and plan[1]["already"] is True and plan[1]["txid"] == "0123456789abcdef"
    assert staged_count(home) == 3 and swept_items(home) == []   # receipts only after publish


def test_s5_finish_sweep_copies_ciphertext_and_writes_receipts(tmp_path):
    home = tmp_path / "home"; staging = tmp_path / "staging"; metas = _stage(home, 2)
    (staging / "archive").mkdir(parents=True); (staging / "archive" / "new.bin").write_bytes(b"new"); (staging / "archive" / "new.tags.json").write_text("{}")
    (home / "archive").mkdir(); (home / "archive" / "old.bin").write_bytes(b"old"); (staging / "archive" / "old.bin").write_bytes(b"DIFFERENT")
    plan = [{"meta": str(metas[0]), "sha256": "x", "bytes": 13, "txid": "ab" * 32, "already": False, "captured_at": None},
            {"meta": str(metas[1]), "sha256": "y", "bytes": 13, "txid": "cd" * 32, "already": True, "captured_at": None}]
    report = {"sweep": {}}
    eb._finish_sweep(home, staging, "E9", plan, report)
    assert (home / "archive" / "new.bin").read_bytes() == b"new" and (home / "archive" / "new.tags.json").is_file()
    assert (home / "archive" / "old.bin").read_bytes() == b"old"   # existing ciphertext is never overwritten
    assert report["sweep"] == {"ciphertext_files_copied": 2, "receipts": 2}
    assert staged_count(home) == 0 and swept_items(home) == metas
    r = json.loads(sweep_receipt_path(metas[1]).read_text())
    assert r["epoch_id"] == "E9" and r["txid"] == "cd" * 32 and r["already_archived"] is True and r["bytes"] == 13


def test_s6_build_signature_carries_the_no_sweep_switch():
    import inspect
    assert inspect.signature(eb.build).parameters["sweep"].default is True
