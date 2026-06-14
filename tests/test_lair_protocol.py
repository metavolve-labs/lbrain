"""Tests for the Lair Protocol commit-check heuristic.

Regression guard for the 2026-05-30 false-negative fix: a decision/commitment
statement must be flagged commit-worthy even when it carries no explicit ISO date.
"""

from lbrain.lair_protocol import should_commit_to_lair


def test_dateless_decision_is_commit_worthy():
    """The original bug: 'we decided ... now the architectural default' scored 0
    because the decision branch was gated on an ISO date being present."""
    sug = should_commit_to_lair(
        "We decided to use Arweave for permanent byte storage and AO Registrar for "
        "amendable state — this is now the architectural default for the Golden Codex substrate."
    )
    assert sug.should_commit is True
    assert sug.confidence >= 0.5
    assert sug.suggested_type == "project"
    assert "No strong signals" not in sug.reasoning


def test_dated_decision_scores_higher_than_dateless():
    """A date is a confidence bonus, not a gate."""
    dateless = should_commit_to_lair("We chose snake_case as the canonical schema convention.")
    dated = should_commit_to_lair(
        "On 2026-05-04 we chose snake_case as the canonical schema convention."
    )
    assert dateless.should_commit is True
    assert dated.confidence > dateless.confidence


def test_commitment_phrase_without_decision_verb():
    """Standard-setting phrasing alone (no 'decided'/'chose') still commits."""
    sug = should_commit_to_lair(
        "Going forward, Cloud Run is the standard approach for stateless services."
    )
    assert sug.should_commit is True
    assert sug.suggested_type in ("project", "feedback")


def test_explicit_save_intent_still_fires():
    sug = should_commit_to_lair("Remember that the GCP project is my-project-12345.")
    assert sug.should_commit is True


def test_routine_text_does_not_over_fire():
    """Guard against false positives: routine status output is not commit-worthy."""
    for routine in (
        "Ran the test suite, all 13 tests passed.",
        "The build finished in 42 seconds.",
        "Here is the file you asked for.",
    ):
        sug = should_commit_to_lair(routine)
        assert sug.should_commit is False, routine
        assert sug.reasoning == "No strong signals." or sug.confidence < 0.5
