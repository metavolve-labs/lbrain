"""An unrecognised embedding provider must RAISE, never resolve to a hosted one.

Regression pin for S8 (CSO fresh-machine audit, 2026-08-03). `make_embedder`
used to end with an unconditional `return EmbedClient(...)` — the OpenAI client
— so any provider value that was not exactly "local" or "gemini" fell through
to it. A single typo in config.toml shipped the user's corpus off-device with
no error anywhere.

These tests assert the PROPERTY (nothing unrecognised reaches a network client),
not the message text, so rewording the error will not break them.
"""
import pytest

from lbrain.config import Config
from lbrain.embed import (
    EmbedClient,
    GeminiEmbedClient,
    KNOWN_PROVIDERS,
    LocalEmbedClient,
    UnknownProviderError,
    make_embedder,
)


def _cfg(provider):
    return Config(embedding_provider=provider, openai_api_key="x", gemini_api_key="y")


# The literal near-misses that motivated the fix: a transposed character, a
# capitalisation slip, a stray space, an empty value, a plausible-but-wrong name.
@pytest.mark.parametrize(
    "bad",
    ["gemni", "Local", "LOCAL", "gemini ", " local", "", "openai2", "claude", "ollama", "none"],
)
def test_unrecognised_provider_raises_instead_of_falling_through(bad):
    with pytest.raises(UnknownProviderError):
        make_embedder(_cfg(bad))


def test_the_fallthrough_target_is_specifically_unreachable():
    """The old bug's landing site. If this regresses, a typo leaks the corpus."""
    try:
        client = make_embedder(_cfg("gemni"))
    except UnknownProviderError:
        return
    pytest.fail(
        f"a typo'd provider resolved to {type(client).__name__} instead of raising — "
        "S8 has regressed and user documents can leave the machine"
    )


def test_local_still_works():
    assert isinstance(make_embedder(_cfg("local")), LocalEmbedClient)


def test_gemini_still_works():
    assert isinstance(make_embedder(_cfg("gemini")), GeminiEmbedClient)


def test_openai_remains_an_explicit_opt_in():
    """Fail-closed must not mean 'openai is gone' — it means it must be ASKED for."""
    assert isinstance(make_embedder(_cfg("openai")), EmbedClient)


def test_every_known_provider_constructs():
    """KNOWN_PROVIDERS is named in the error message; keep it honest."""
    for name in KNOWN_PROVIDERS:
        assert make_embedder(_cfg(name)) is not None


def test_missing_key_falls_back_without_reaching_openai():
    """A config that omits the field entirely resolves to the dataclass default.

    Pinned deliberately: the default stays "gemini" because anyone relying on it
    embedded at 1536 dims under that same fallback, and flipping it to "local"
    (384) would silently corrupt their vector space — the A-405 failure. What
    matters for S8 is only that it is never OpenAI.
    """
    client = make_embedder(Config(gemini_api_key="y"))
    # EmbedClient is the OpenAI client and the other two do NOT subclass it,
    # so this is an exact "did not reach OpenAI" assertion, not a loose one.
    assert not isinstance(client, EmbedClient)
