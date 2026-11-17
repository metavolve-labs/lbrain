"""FX-178 residue (Sostra 2026-11-17T05:19Z): `lbrain whoami` against a home that does not exist must say so and
exit 2, never describe it as a fully functional local brain, and never create it."""
import json
import os
import subprocess
import sys


def test_whoami_missing_home_reports_and_exits_2(tmp_path):
    home = tmp_path / "no-such-home"
    env = dict(os.environ, LBRAIN_HOME=str(home))
    env.pop("LBRAIN_CREATE_HOME", None)
    r = subprocess.run([sys.executable, "-m", "lbrain.cli", "whoami", "--json"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, (r.returncode, r.stdout[-300:], r.stderr[-300:])
    body = r.stdout[r.stdout.index("{"):]
    payload = json.loads(body)
    assert payload["error"] == "home missing"
    assert str(home) in payload["path"]
    assert not home.exists()
