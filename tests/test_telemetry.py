"""Telemetry exporter — brain health as scrapeable metrics (Fable's module).

The store gauges must be correct, the Prometheus format must be valid, and a
metric that can't be measured (currency without config) must be OMITTED, not
reported as a false zero.
"""
import json

from click.testing import CliRunner

from lbrain import telemetry
from lbrain.cli import main
from lbrain.config import Config
from lbrain.index import chunk, parse
from lbrain.store import Store


def _populated_store(tmp_path):
    """A store with two docs (and their chunks + a wikilink edge), at the
    isolated home's db path — realistic enough for the gauges to be non-trivial."""
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    (corpus / "a.md").write_text("---\nname: a\n---\n# A\n\nalpha links to [[b]].\n")
    (corpus / "b.md").write_text("# B\n\nbravo content here.\n")
    st = Store(Config.load().db_path)
    for rel in ("a.md", "b.md"):
        doc = parse(corpus / rel, repo_root=corpus)
        st.upsert_doc(doc)
        st.insert_chunks(chunk(doc))
        st.replace_wikilinks(doc)
    st.db.commit()
    return st


def test_collect_store_metrics(tmp_path):
    st = _populated_store(tmp_path)
    m = telemetry.collect_metrics(st)  # no cfg → store-only gauges
    st.close()
    assert m["lbrain_docs_total"] == 2
    assert m["lbrain_chunks_total"] >= 2
    assert m["lbrain_wikilinks_total"] >= 1
    assert 0.0 <= m["lbrain_embedding_coverage_ratio"] <= 1.0
    # currency/drift need cfg — they must be OMITTED, not guessed as 0
    assert "lbrain_index_current" not in m
    assert "lbrain_embedding_drift" not in m


def test_render_prometheus_is_valid(tmp_path):
    st = _populated_store(tmp_path)
    text = telemetry.render_prometheus(telemetry.collect_metrics(st))
    st.close()
    assert "# HELP lbrain_docs_total" in text
    assert "# TYPE lbrain_docs_total gauge" in text
    assert 'lbrain_build_info{version="' in text
    # every non-comment line is "metric_name <numeric value>"
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            assert name, f"no metric name in {line!r}"
            float(value)  # value must parse as a number


def test_zero_chunks_no_division_error():
    class _Empty:
        def stats(self):
            return {"docs": 0, "chunks": 0, "embedded": 0,
                    "wikilinks": 0, "priority_docs": 0, "archives": 0}
    m = telemetry.collect_metrics(_Empty())
    assert m["lbrain_embedding_coverage_ratio"] == 0.0
    assert m["lbrain_chunks_total"] == 0


def test_cli_metrics_prometheus_and_json(tmp_path):
    # Click 8.2+ keeps stderr separate by default, so .output is stdout only —
    # which is the contract: metrics stdout stays clean (machine-parseable),
    # the unprovisioned warning goes to stderr.
    _populated_store(tmp_path).close()
    r1 = CliRunner().invoke(main, ["metrics", "--no-currency"])
    assert r1.exit_code == 0, r1.output
    assert "lbrain_docs_total 2" in r1.output
    assert "# TYPE lbrain_build_info gauge" in r1.output

    r2 = CliRunner().invoke(main, ["metrics", "--no-currency", "--format", "json"])
    assert r2.exit_code == 0, r2.output
    # an unprovisioned test home prints a stderr warning that CliRunner may fold
    # into .output; the JSON object is what we assert on, so slice from its start.
    payload = json.loads(r2.output[r2.output.index("{"):])
    assert payload["metrics"]["lbrain_docs_total"] == 2
    assert "version" in payload


def test_currency_metrics_appear_with_config(tmp_path):
    st = _populated_store(tmp_path)
    cfg = Config.load()
    cfg.sources = [tmp_path / "corpus"]  # a real source dir the survey can scan
    m = telemetry.collect_metrics(st, cfg)
    st.close()
    assert "lbrain_index_current" in m
    assert m["lbrain_index_current"] in (0, 1)
    assert "lbrain_embedding_drift" in m
