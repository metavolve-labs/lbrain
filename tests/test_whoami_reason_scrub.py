"""`whoami`'s `unknown (<reason>)` must be one scrubbed, capped line, never the raw exception.

CSO review of PR #87 (2026-10-17T2234Z): an exception raised inside open_store() carrying a
fake `sk-ant-...` key, an env assignment and a newline came out verbatim in the CLI and in
`lair_whoami`. The reason is now passed through a conservative key-shape scrub, folded to a
single line and capped at about 200 characters, on every surface (describe() feeds both).
"""
from __future__ import annotations

import json
import os
import subprocess

from lbrain import epoch as ep
from lbrain.config import Config
from lbrain.identity import describe, read_counts, scrub_reason

INJECTED = ("auth failed for key sk-ant-api03-FAKESECRET123abc env ANTHROPIC_API_KEY=xyz\n"
            "second line /root/.ssh/id_rsa")


def _reason_via_read_counts(monkeypatch, msg: str) -> str:
    class Boom(Exception):
        pass

    def bad(cfg, **k):
        raise Boom(msg)

    monkeypatch.setattr(ep, "open_store", bad)
    stats, err = read_counts(object())
    assert stats == {}
    return err


def _surface(tmp_path, monkeypatch, err: str) -> str:
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    return describe(Config.load(), {}, counts_error=err)["brain"]["counts_unknown"]


def test_an_injected_api_key_is_redacted(tmp_path, monkeypatch):
    out = _surface(tmp_path, monkeypatch, _reason_via_read_counts(monkeypatch, INJECTED))
    assert "FAKESECRET123" not in out and "sk-ant-api03" not in out, out
    assert "[REDACTED]" in out


def test_an_env_assignment_value_is_redacted(tmp_path, monkeypatch):
    out = _surface(tmp_path, monkeypatch, _reason_via_read_counts(monkeypatch, INJECTED))
    assert "=xyz" not in out and "xyz" not in out.split("ANTHROPIC_API_KEY", 1)[-1][:12], out
    assert "ANTHROPIC_API_KEY=[REDACTED]" in out, out


def test_a_newline_cannot_open_a_second_line(tmp_path, monkeypatch):
    out = _surface(tmp_path, monkeypatch, _reason_via_read_counts(monkeypatch, INJECTED))
    assert "\n" not in out and "\r" not in out, repr(out)
    assert "second line" in out  # folded onto the one line, not dropped


def test_the_reason_is_capped(tmp_path, monkeypatch):
    out = _surface(tmp_path, monkeypatch, "x " * 1000)
    assert len(out) <= 201, len(out)


def test_other_key_shapes_are_redacted():
    for secret in ("AIzaSyA1234567890abcdefghijklmnopqrstu", "ghp_abcdefghijklmnopqrstuvwx",
                   "xoxb-123456789012-abcdefghijkl", "AKIAABCDEFGHIJKLMNOP",
                   "Bearer abc.def.ghi-jkl", "-----BEGIN RSA PRIVATE KEY-----"):
        out = scrub_reason(f"failed with {secret} here")
        assert secret not in out, (secret, out)
        assert "[REDACTED]" in out


def test_a_long_opaque_token_without_a_known_prefix_is_redacted():
    token = "Zx9q" * 12   # 48 chars, no vendor prefix
    out = scrub_reason(f"server said {token} then closed")
    assert token not in out and "[REDACTED]" in out, out
    assert "server said" in out and "then closed" in out


def test_cli_and_mcp_both_print_the_scrubbed_line(tmp_path, monkeypatch):
    """Both surfaces render describe(); the injected text must not survive on either."""
    import lbrain.cli as cli
    import lbrain.config as config
    from click.testing import CliRunner
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    for mod in (config, cli):
        monkeypatch.setattr(mod, "CONFIG_DIR", tmp_path, raising=False)
        monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "config.toml", raising=False)

    def bad(cfg, **k):
        raise RuntimeError(INJECTED)

    monkeypatch.setattr(ep, "open_store", bad)
    res = CliRunner().invoke(cli.main, ["whoami"])
    line = [ln for ln in res.output.splitlines() if "indexed:" in ln]
    assert len(line) == 1, res.output
    assert "FAKESECRET" not in res.output and "=xyz" not in res.output, res.output
    assert "second line" in line[0]  # same line, not a new one
    from lbrain import mcp_server
    out = mcp_server.lair_whoami()
    payload = json.loads(out[out.index("{"):])
    cu = payload["brain"]["counts_unknown"]
    assert "FAKESECRET" not in cu and "\n" not in cu and "[REDACTED]" in cu, cu


# ---------- normal path (control) ----------

def test_an_ordinary_reason_passes_through_unchanged_in_substance(tmp_path, monkeypatch):
    msg = ("CURRENT names epoch '20000101T000000Z-1-0001' but /home/x/epochs/20000101T000000Z-1-0001/"
           "brain.db does not exist")
    out = _surface(tmp_path, monkeypatch, _reason_via_read_counts(monkeypatch, msg))
    assert out.startswith("store did not open: Boom: CURRENT names epoch")
    assert "does not exist" in out and "[REDACTED]" not in out, out


def test_full_counts_still_have_no_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path))
    b = describe(Config.load(), {"docs": 1, "chunks": 2, "embedded": 2})["brain"]
    assert b["counts_unknown"] == ""


def test_real_dangling_current_reason_still_names_the_cause(tmp_path):
    """End to end on a real epoch-shaped home: the scrub must not eat the actual cause."""
    home = tmp_path / "home"
    (home / "epochs").mkdir(parents=True)
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\nsources = []\n')
    (home / "epochs" / "CURRENT").write_text("20000101T000000Z-1-0001\n")
    p = subprocess.run(["lbrain", "whoami"], env=dict(os.environ, LBRAIN_HOME=str(home)),
                       capture_output=True, text=True)
    line = [ln for ln in p.stdout.splitlines() if "indexed:" in ln]
    assert len(line) == 1 and "unknown (" in line[0] and "does not exist" in line[0], p.stdout + p.stderr
