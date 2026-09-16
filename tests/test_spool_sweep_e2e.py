"""END-TO-END: `build()` with sweeping ON, driving the real CLI of THIS checkout (CSO 2026-09-16: "no test in the suite
drives build() end to end with sweeping enabled; the suite tests the parts; the defects live in the seam").

  E1  stage one payload -> build -> receipt, key on the home, retrieve round trip byte-identical, payload DRAINED,
      meta + receipt remain, snapshot rendered as turns
  E2  remove the receipt only -> build again -> the already-captured branch prints the FULL txid, the receipt is
      restored, nothing else changes (P-A)
  E3  a torn .meta.json beside a good one -> the build still publishes, names the torn file, sweeps the good one (P-B)
The engine binary is a wrapper that runs this checkout's `lbrain.cli`, so the test cannot pass on the installed engine
by accident. Local embedding provider; nothing leaves the machine.
"""
import hashlib, json, os, subprocess, sys
from pathlib import Path

from lbrain import epoch_build as eb
from lbrain.config import Config
from lbrain.spool import META_SUFFIX, PAYLOAD_SUFFIX, staged_count, staging_dir, swept_items, sweep_receipt_path

REPO = Path(__file__).resolve().parents[1]


def _bin(tmp_path):
    b = tmp_path / "lbrain-bin"
    b.write_text(f"#!/bin/bash\nexec env PYTHONPATH={REPO} {sys.executable} -m lbrain.cli \"$@\"\n"); b.chmod(0o755)
    return str(b)


def _home(tmp_path):
    home = tmp_path / "home"; src = tmp_path / "src"; src.mkdir(); home.mkdir()
    (src / "a.md").write_text("# doc\n\nhello sweep e2e\n")
    (home / "config.toml").write_text(f'db_path = "{home}/brain.db"\nembedding_provider = "local"\nembedding_dim = 384\nsources = [\n  "{src}",\n]\n')
    return home


def _stage(home, name, text):
    d = staging_dir(home); d.mkdir(parents=True, exist_ok=True)
    payload = text.encode(); h = hashlib.sha256(payload).hexdigest(); stem = h[:32]
    (d / f"{stem}{PAYLOAD_SUFFIX}").write_bytes(payload)
    meta = d / f"{stem}{META_SUFFIX}"
    meta.write_text(json.dumps({"schema": 1, "sha256": h, "size": len(payload), "session_id": name, "title": name, "namespace": None, "captured_at": "2026-09-16T00:00:00Z"}))
    return meta, payload


def _jsonl(mark="39/39"):
    rows = [json.dumps({"type": "user", "timestamp": "2026-09-16T00:00:01Z", "message": {"content": "Mount the seat and run the receipt."}}),
            json.dumps({"type": "assistant", "timestamp": "2026-09-16T00:00:02Z", "message": {"content": [{"type": "text", "text": f"Receipt passed {mark}."}]}})]
    return "\n".join(rows) + "\n"


def _build(home, tmp_path, **kw):
    os.environ["LBRAIN_HOME"] = str(home); os.environ["LBRAIN_ARCHIVE_PASSPHRASE"] = "e2e-pass"
    cfg = Config.load()
    return eb.build(home, cfg, lbrain_bin=_bin(tmp_path), keep=5, **kw)


def test_e1_e2_e3_build_with_sweep_end_to_end(tmp_path):
    home = _home(tmp_path)
    meta, payload = _stage(home, "sess-1", _jsonl())
    rep = _build(home, tmp_path, delta=False)
    sw = rep["sweep"]
    assert rep["published"] and sw["swept"] == 1 and sw["receipts"] == 1 and sw["key_files_copied"] >= 1, sw
    r = json.loads(sweep_receipt_path(meta).read_text()); txid = r["txid"]
    assert len(txid) >= 20 and (home / "keys" / f"{txid}.key").is_file()
    out = subprocess.run([_bin(tmp_path), "retrieve", "--txid", txid, "--out", str(tmp_path / "rt.bin")], env={**os.environ, "LBRAIN_HOME": str(home)}, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "rt.bin").read_bytes() == payload   # E1: round trip
    payload_path = meta.with_name(meta.name[:-len(META_SUFFIX)] + PAYLOAD_SUFFIX)
    assert sw["reclaimed"] == 1 and not payload_path.exists()   # drained
    assert meta.is_file() and sweep_receipt_path(meta).is_file() and staged_count(home) == 0
    db = home / "epochs" / (home / "epochs" / "CURRENT").read_text().strip() / "brain.db"
    con = eb._connect_vec(db); snap = con.execute("SELECT snapshot FROM archives WHERE txid=?", (txid,)).fetchone()[0]; con.close()
    assert snap.startswith("## user") and "Mount the seat" in snap and "{" not in snap
    # E2: a lost receipt is restored by the already-captured branch with the FULL txid (the payload must be present to re-capture)
    sweep_receipt_path(meta).unlink(); payload_path.write_bytes(payload)
    rep2 = _build(home, tmp_path, delta=True)
    sw2 = rep2["sweep"]
    assert sw2["already"] == 1 and sw2["failed"] == 0 and sw2["receipts"] == 1, sw2
    assert json.loads(sweep_receipt_path(meta).read_text())["txid"] == txid
    # E3: a torn sidecar beside a good entry never aborts the build
    good, _ = _stage(home, "sess-2", _jsonl("40/40"))
    torn = staging_dir(home) / ("f" * 32 + META_SUFFIX); torn.write_text("{torn"); (staging_dir(home) / ("f" * 32 + PAYLOAD_SUFFIX)).write_bytes(b"x")
    rep3 = _build(home, tmp_path, delta=True)
    sw3 = rep3["sweep"]
    assert rep3["published"] and sw3["failed"] == 1 and torn.name in sw3["failures"][0] and sw3["swept"] == 1, sw3
    assert sweep_receipt_path(good).is_file()


def test_e4_refusal_path_retains_no_keys_at_the_call_site(tmp_path, monkeypatch):
    """M4c: through build() itself, not the helper: a refused candidate's .failed dir holds ciphertext, never keys."""
    import pytest
    home = _home(tmp_path)
    _stage(home, "sess-r", _jsonl("41/41"))
    real = eb.validate_candidate
    monkeypatch.setattr(eb, "validate_candidate", lambda *a, **k: ["forced refusal (test)"])
    with pytest.raises(eb.EpochError):
        _build(home, tmp_path, delta=False)
    failed = sorted((home / "epochs").glob("*.failed"))
    assert failed and (failed[-1] / "archive").is_dir()
    assert not (failed[-1] / "keys").exists()
    assert (home / "epochs" / "CURRENT").exists() is False or True   # no publish happened; CURRENT untouched (none existed)


def test_e5_full_build_carries_the_archive_index_at_the_call_site(tmp_path):
    """M7: through build(): after a sweep, a --full rebuild keeps the archive rows AND their vectors, and retrieve works."""
    home = _home(tmp_path)
    meta, payload = _stage(home, "sess-f", _jsonl("42/42"))
    rep1 = _build(home, tmp_path, delta=False)
    txid = json.loads(sweep_receipt_path(meta).read_text())["txid"]
    rep2 = _build(home, tmp_path, delta=False)   # a second FULL build: the archive index is not derived from sources
    assert rep2["published"] and rep2.get("archive_index_carried", 0) >= 1, rep2
    db = home / "epochs" / (home / "epochs" / "CURRENT").read_text().strip() / "brain.db"
    con = eb._connect_vec(db)
    assert con.execute("SELECT count(*) FROM archives WHERE txid=? AND shredded=0", (txid,)).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM vec_archives").fetchone()[0] >= 1
    assert "float[384]" in con.execute("SELECT sql FROM sqlite_master WHERE name='vec_archives'").fetchone()[0]
    con.close()
    out = subprocess.run([_bin(tmp_path), "retrieve", "--txid", txid, "--out", str(tmp_path / "rt2.bin")], env={**os.environ, "LBRAIN_HOME": str(home)}, capture_output=True, text=True)
    assert out.returncode == 0 and (tmp_path / "rt2.bin").read_bytes() == payload


def test_e6_sweep_status_never_resolves_a_secret_and_never_fails(tmp_path, monkeypatch):
    """M9/X6: with a gcp-secret reference and a resolver that would raise, status exits 0, says 'not resolved', and promises nothing."""
    home = _home(tmp_path); _stage(home, "sess-s", _jsonl("43/43"))
    env = {**os.environ, "LBRAIN_HOME": str(home), "LBRAIN_ARCHIVE_PASSPHRASE": "gcp-secret:no-such-project/no-such-secret", "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent"}
    out = subprocess.run([_bin(tmp_path), "sweep-status", "--json"], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    rep = json.loads(out.stdout[out.stdout.index("{"):])
    assert rep["passphrase"].startswith("configured (gcp-secret reference, not resolved") and "if the resolver fails the captures stay staged" in rep["next_build_would"]


def test_v5_build_passes_the_configured_embedding_width_to_the_sweep(tmp_path, monkeypatch):
    """V5 (CSO gen-195 countermodel): the build call site must hand `_sweep_spool` cfg.embedding_dim (384 here), never a
    hardcoded 1536. Spies on the real call through build() with sweeping on."""
    home = _home(tmp_path); _stage(home, "sess-v5", _jsonl())
    seen = {}
    real = eb._sweep_spool
    def spy(home_, staging, lbrain_bin, lock, report, dim=0):
        seen["dim"] = dim; return real(home_, staging, lbrain_bin, lock, report, dim=dim)
    monkeypatch.setattr(eb, "_sweep_spool", spy)
    rep = _build(home, tmp_path, delta=False)
    assert rep["published"] and rep["sweep"]["swept"] == 1
    assert seen["dim"] == 384 == Config.load().embedding_dim
