"""Lair Protocol — decide what's lair-worthy and guard against feedback conflicts.

Two pure-heuristic, zero-LLM responsibilities:
1. ``should_commit_to_lair`` — score whether a piece of text is worth saving.
2. ``detect_anti_pattern`` — warn when a proposed action conflicts with saved feedback.

(The former "Cognitive Nutrition" preamble — which injected hardcoded, project-specific
directives into the agent ahead of search results — was removed 2026-06-07: it biased the
agent toward opinions the user never stored. Memory should surface what's saved, not editorialize.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .search import Hit


@dataclass
class LairCommitSuggestion:
    should_commit: bool
    confidence: float
    suggested_type: str  # user/feedback/project/reference
    suggested_slug: str
    reasoning: str


def should_commit_to_lair(text: str) -> LairCommitSuggestion:
    """Decide whether a piece of conversation text should become a lair/memory entry.

    Heuristics:
    - Strong signals: user said 'remember', 'save this', 'don't forget', explicit decision/feedback
    - Medium: surprising fact, non-obvious choice, name+date+constraint cluster
    - Weak: routine task output

    A decision or commitment is commit-worthy whether or not it carries an explicit
    date — the date is a confidence bonus, not a gate. (Pre-2026-05-30 this block only
    fired when an ISO date was also present, so plain "we decided X is now the default"
    scored 0 and was a false-negative.)
    """
    t = text.lower()
    score = 0.0
    reasoning: list[str] = []
    suggested_type = "project"

    # Strong signals (explicit user intent)
    if re.search(r"\b(remember|save this|don't forget|note this|tag this|memorize)\b", t):
        score += 0.85
        reasoning.append("Explicit user save-intent.")

    # Decision / commitment signals — captured regardless of whether a date is present.
    decision_verb = re.search(
        r"\b(decid(?:e|es|ed|ing)|decision|chose|chosen|choosing|"
        r"settl(?:e|es|ed|ing) on|go(?:ing)? with|went with|"
        r"lock(?:ed|ing)?|approv(?:e|es|ed)|finaliz(?:e|es|ed)|agreed)\b",
        t,
    )
    commitment_phrase = re.search(
        r"(architectural default|the (?:new )?default|now the default|"
        r"is now the\b|going forward|from now on|moving forward|"
        r"canonical|source of truth|standard (?:practice|approach)|"
        r"the (?:standard|convention) is)",
        t,
    )
    has_date = re.search(r"\b(2026|2027)-\d{2}-\d{2}\b", t)
    if decision_verb or commitment_phrase:
        score += 0.5
        suggested_type = "project"
        if decision_verb and commitment_phrase:
            score += 0.2
            reasoning.append("Decision + commitment phrasing.")
        elif decision_verb:
            reasoning.append("Decision/commitment language.")
        else:
            reasoning.append("Commitment / standard-setting phrasing.")
        if has_date:
            score += 0.2
            reasoning.append("Date-bound.")

    # Feedback signals
    if re.search(r"\b(don't|stop|prefer|always|never|going forward|from now on)\b", t):
        score += 0.4
        reasoning.append("Behavioral feedback pattern.")
        suggested_type = "feedback"

    # User-profile signals
    if re.search(r"\b(i am|i'm a|my role|my background|i prefer|i'm working on)\b", t):
        score += 0.35
        reasoning.append("User-profile signal.")
        suggested_type = "user"

    # Reference signals
    if re.search(r"\b(at|in|see) (https?://|gs://|/mnt/|c:\\)", t, re.IGNORECASE):
        score += 0.2
        reasoning.append("External-reference signal.")
        suggested_type = "reference"

    # Surprising / non-obvious markers
    if re.search(r"\b(surprising|non-obvious|gotcha|caveat|workaround|did not expect)\b", t):
        score += 0.3
        reasoning.append("Surprise/gotcha marker.")

    confidence = min(score, 1.0)
    should = confidence >= 0.5
    slug = _suggest_slug(text, suggested_type)

    return LairCommitSuggestion(
        should_commit=should,
        confidence=confidence,
        suggested_type=suggested_type,
        suggested_slug=slug,
        reasoning="; ".join(reasoning) or "No strong signals.",
    )


def _suggest_slug(text: str, doc_type: str) -> str:
    """Best-effort kebab-case slug from the first sentence."""
    sent = text.strip().split("\n")[0][:80]
    words = re.findall(r"[A-Za-z0-9]+", sent.lower())[:6]
    return f"{doc_type}_" + "-".join(words) if words else f"{doc_type}_entry"


_STOPWORDS = frozenset(
    """
    about above after again against also been before being below between both could does
    doing down during each every from have having here itself just just like more most
    much must once only other over same should some such than that them then there these
    they this those through under until very what when where which while with would your
    yours user when with this they that this used into upon also other than
    rule when never always cannot stop avoid this with they have when where
    """.split()
)


def detect_anti_pattern(action_description: str, feedback_hits: list[Hit]) -> list[str]:
    """Cross-check a proposed action against feedback_*.md rules in the brain.

    Returns a list of human-readable warnings if the action looks like it conflicts with
    saved feedback. Empty list = no conflicts detected.
    """
    warnings: list[str] = []
    action_low = action_description.lower()
    action_nouns = {
        w for w in re.findall(r"[a-z][a-z\-]{3,}", action_low) if w not in _STOPWORDS
    }
    for hit in feedback_hits:
        if hit.doc_type != "feedback":
            continue
        for line in hit.text.split("\n"):
            ll = line.lower()
            if any(neg in ll for neg in ["don't", "never", "do not", "stop ", "avoid "]):
                rule_nouns = {
                    w
                    for w in re.findall(r"[a-z][a-z\-]{3,}", ll)
                    if w not in _STOPWORDS
                }
                overlap = rule_nouns & action_nouns
                if len(overlap) >= 3:
                    # Both fields are corpus-derived. Unsanitized, a note body
                    # carrying \r / U+2028 / a homoglyph fence forges a second
                    # ⚠️ line at column 0 inside the caller's output
                    # (red-team 2026-07-28, finding 1).
                    from .serve import sanitize_field

                    warnings.append(
                        f"⚠️ {sanitize_field(hit.rel_path, 160)}: "
                        f"'{sanitize_field(line.strip(), 140)}' "
                        f"(overlap: {', '.join(sorted(overlap))})"
                    )
                    break
    return warnings
