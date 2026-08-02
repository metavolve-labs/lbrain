"""Config.write → Config.load round-trip — every dataclass field survives.

Red-team 2026-07-24: write() is a manually-maintained line list and had already
drifted (abstraction_topk_cap / abstraction_recency_guard were loaded but never
persisted), which means a rollback value written to config could be silently
resurrected to the class default by any later cfg.write(). This test is the
hard gate against that class of drift: set every field to a NON-default value,
write, reload, compare.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from lbrain import config as config_mod
from lbrain.config import Config

# Secrets round-trip through ~/.lbrain/env + os.environ, not config.toml —
# excluded here (their write path is _write_env_var, covered elsewhere).
EXCLUDED = {"openai_api_key", "gemini_api_key"}


def non_default(name: str, value):
    """A deterministic non-default value of the same type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return value + 0.125
    if isinstance(value, str):
        if name == "gemini_base_url":
            return "https://proxy.example.com/v1beta"
        if name == "serve_mode":
            # "prose", NOT "structured" — structured became the DEFAULT on
            # 2026-07-28 (the answer-presence A/B it was waiting on had already
            # been run and shipped), which made this gate vacuous for the field.
            # Same failure mode as embedding_provider below.
            return "prose"
        if name == "embedding_provider":
            # "openai", NOT "gemini" — gemini became the DEFAULT on 2026-07-25
            # (code defaults realigned to the deployed GCP-native config), which
            # made this gate vacuous for the field. Same failure mode as
            # arweave_transport below; the assert at the bottom caught it.
            return "openai"
        if name == "arweave_transport":
            # "arweave", NOT "local" — "local" IS the default, which made this
            # gate vacuous for the field (2026-07-24 review finding)
            return "arweave"
        if name == "archive_namespace":
            return "shared"
        return value + "-x"
    if isinstance(value, Path):
        return Path(str(value) + ".alt")
    if isinstance(value, list):
        # `sources` is the only list of Paths; the disclosure allowlists are
        # lists of STRINGS and must round-trip as strings. Handing them a Path
        # sentinel made the gate fail for the right reason (write/load really did
        # differ) but for the wrong field — so distinguish by name rather than
        # relaxing the comparison, which would stop the gate catching a real drop.
        if name in ("allowed_doc_types", "allowed_path_prefixes"):
            return ["TOPIC-AREA/"]
        return [Path("/tmp/lbrain-test-source")]
    raise AssertionError(f"unhandled field type for {name}: {type(value)}")


def test_write_load_roundtrip_every_field(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "env")

    cfg = Config()
    for f in dataclasses.fields(Config):
        if f.name in EXCLUDED:
            continue
        nd = non_default(f.name, getattr(cfg, f.name))
        # a "non-default" equal to the default makes the gate vacuous for
        # that field — fail loudly instead of silently weakening the test
        assert nd != getattr(cfg, f.name), f"non_default collides with default: {f.name}"
        setattr(cfg, f.name, nd)

    cfg.write()
    loaded = Config.load()

    drifted = []
    for f in dataclasses.fields(Config):
        if f.name in EXCLUDED:
            continue
        want, got = getattr(cfg, f.name), getattr(loaded, f.name)
        # sources/db_path round-trip as Paths
        if want != got:
            drifted.append((f.name, want, got))
    assert not drifted, (
        "Config.write() dropped or altered fields (add them to the write() "
        f"line list!): {drifted}"
    )


def test_init_gives_new_installs_structured_serving(tmp_path, isolate_lbrain_home):
    """A fresh install must produce the output the README documents.

    The code default is "prose" deliberately (fail-open + one-line rollback), so
    without an explicit write at init a stranger runs the documented query and
    gets flat results with no `binds` annotation — the product's whole claim,
    invisible. Regression guard for exactly that.
    """
    from click.testing import CliRunner
    import lbrain.cli as cli
    home = isolate_lbrain_home
    src = tmp_path / "notes"; src.mkdir()

    res = CliRunner().invoke(cli.main, ["init", "--provider", "local", "--source", str(src)])
    assert res.exit_code == 0, res.output
    assert 'serve_mode = "structured"' in (home / "config.toml").read_text(encoding="utf-8")


def test_init_does_not_switch_an_existing_install(tmp_path, isolate_lbrain_home):
    """Re-running init on a configured brain must not change how it serves."""
    from click.testing import CliRunner
    import lbrain.cli as cli
    home = isolate_lbrain_home
    (home / "config.toml").write_text('serve_mode = "prose"\n', encoding="utf-8")
    src = tmp_path / "notes"; src.mkdir()

    res = CliRunner().invoke(cli.main, ["init", "--provider", "local", "--source", str(src)])
    assert res.exit_code == 0, res.output
    assert 'serve_mode = "prose"' in (home / "config.toml").read_text(encoding="utf-8")


def test_ambient_api_key_is_not_consent_to_use_a_remote_provider(tmp_path, isolate_lbrain_home, monkeypatch):
    """An exported GEMINI_API_KEY must not ship the corpus to Google.

    Before 2026-07-27 `init` treated a key found anywhere — including the ambient
    environment, via `--gemini-key`'s envvar and Config.load() — as a request to
    embed remotely. A developer with that variable already exported ran the
    documented command and had every document sent to a third party, while the
    README promised nothing left their machine. Remote is opt-in, by flag only.
    """
    from click.testing import CliRunner
    import lbrain.cli as cli

    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key-must-be-ignored")
    src = tmp_path / "notes"; src.mkdir()
    res = CliRunner().invoke(cli.main, ["init", "--source", str(src)])
    assert res.exit_code == 0, res.output
    cfg_text = (isolate_lbrain_home / "config.toml").read_text(encoding="utf-8")
    assert 'embedding_provider = "local"' in cfg_text, cfg_text
    assert "NOT used" in res.output


def test_explicit_key_flag_still_selects_the_hosted_provider(tmp_path, isolate_lbrain_home):
    """Opt-in must keep working — the fix restricts consent, it doesn't remove it."""
    from click.testing import CliRunner
    import lbrain.cli as cli

    src = tmp_path / "notes"; src.mkdir()
    res = CliRunner().invoke(
        cli.main, ["init", "--gemini-key", "explicitly-passed", "--source", str(src)])
    assert res.exit_code == 0, res.output
    assert 'embedding_provider = "gemini"' in (isolate_lbrain_home / "config.toml").read_text(encoding="utf-8")
