"""RSI-PRELIM-1 engine landmine OFF-13: tiktoken fetched at MODULE SCOPE.

A local-first engine must import with no network. `ENCODER = get_encoding(...)`
at import did a CDN fetch, so a cold ~/.tiktoken + restricted egress made the
brain unimportable. The encoder must load lazily; token counting must still be
correct once it is available.
"""
import os
import subprocess
import sys


def _import_with_broken_get_encoding():
    """Simulate cold cache + no egress: get_encoding raises, then import lbrain."""
    code = (
        "import tiktoken\n"
        "def boom(*a, **k):\n"
        "    raise RuntimeError('network blocked (simulated cold tiktoken cache)')\n"
        "tiktoken.get_encoding = boom\n"
        "import lbrain.index\n"
        "import lbrain.search\n"
        "print('IMPORT_OK')\n"
    )
    env = dict(os.environ, LBRAIN_HOME="/tmp/lbrain-off13-test")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_off13_import_succeeds_without_encoder_fetch():
    r = _import_with_broken_get_encoding()
    assert "IMPORT_OK" in r.stdout, f"import fetched the encoder at module scope:\n{r.stderr[-600:]}"


def test_off13_token_counting_correct_when_encoder_available():   # NO-REGRESSION
    from lbrain.index import _encoder
    enc = _encoder()
    assert enc.encode("hello world") == enc.encode("hello world")
    assert len(enc.encode("one two three four five")) >= 4
    assert enc.decode(enc.encode("roundtrip")) == "roundtrip"
