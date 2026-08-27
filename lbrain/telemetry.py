"""Telemetry exporter — brain health as scrapeable metrics.

Turns the same signals ``doctor`` inspects into a metrics surface — Prometheus
text exposition format, or JSON — so a local-first brain can be watched by
ops tooling without bolting a server onto it. READ-ONLY: it observes the store,
never mutates it. A metric that cannot be measured is OMITTED, never reported as
a false zero (the same discipline ``doctor`` holds: absence is not health).
"""
from __future__ import annotations

from . import __version__

# Drift statuses from Store.embedding_config_status that mean the stored vectors
# no longer match the live config — the ones worth alerting on. 'match'/'unset'
# are fine.
_DRIFT_STATES = {"model_changed", "dim_changed"}

# HELP text + metric type, for the Prometheus exposition format.
_META = {
    "lbrain_docs_total": ("Documents indexed in the brain.", "gauge"),
    "lbrain_chunks_total": ("Chunks in the brain.", "gauge"),
    "lbrain_chunks_embedded": ("Chunks with a real stored vector.", "gauge"),
    "lbrain_embedding_coverage_ratio": ("Fraction of chunks with a real vector (0-1).", "gauge"),
    "lbrain_wikilinks_total": ("Wikilink edges in the graph.", "gauge"),
    "lbrain_priority_docs_total": ("Priority-flagged documents.", "gauge"),
    "lbrain_archives_total": ("Live (un-shredded) Tier-2 archives.", "gauge"),
    "lbrain_embedding_drift": ("1 if stored vectors no longer match the live embedding config.", "gauge"),
    "lbrain_index_current": ("1 if the index is fully current with its sources.", "gauge"),
    "lbrain_index_divergent_total": ("Records an import would change.", "gauge"),
}


def collect_metrics(store, cfg=None) -> dict:
    """Brain-health metrics from the store (and config, when given).

    Store-only gauges are always present. Currency and embedding-drift read the
    configured sources + embedding config, so they appear only when ``cfg`` is
    passed — absent it, they are omitted rather than guessed.
    """
    s = store.stats()
    chunks = s.get("chunks", 0) or 0
    embedded = s.get("embedded", 0) or 0
    metrics: dict = {
        "lbrain_docs_total": s.get("docs", 0),
        "lbrain_chunks_total": chunks,
        "lbrain_chunks_embedded": embedded,
        "lbrain_embedding_coverage_ratio": round(embedded / chunks, 6) if chunks else 0.0,
        "lbrain_wikilinks_total": s.get("wikilinks", 0),
        "lbrain_priority_docs_total": s.get("priority_docs", 0),
        "lbrain_archives_total": s.get("archives", 0),
    }
    if cfg is not None:
        model = cfg.embedding_model
        if cfg.embedding_provider == "gemini" and not model.startswith("models/"):
            model = f"models/{model}"  # match doctor's like-for-like normalization
        status = store.embedding_config_status(cfg.embedding_dim, model, cfg.embedding_provider)
        metrics["lbrain_embedding_drift"] = 1 if status in _DRIFT_STATES else 0
        from . import index_currency

        survey = index_currency.survey(store, cfg.sources)
        metrics["lbrain_index_current"] = 1 if survey.is_current else 0
        metrics["lbrain_index_divergent_total"] = survey.divergent
    return metrics


def render_prometheus(metrics: dict, version: str = __version__) -> str:
    """Render metrics in Prometheus text exposition format (v0.0.4)."""
    lines = [
        "# HELP lbrain_build_info Build metadata (constant 1; version carried in the label).",
        "# TYPE lbrain_build_info gauge",
        f'lbrain_build_info{{version="{version}"}} 1',
    ]
    for name, value in metrics.items():
        help_text, mtype = _META.get(name, ("", "gauge"))
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        v = value if isinstance(value, float) else int(value)
        lines.append(f"{name} {v}")
    return "\n".join(lines) + "\n"
