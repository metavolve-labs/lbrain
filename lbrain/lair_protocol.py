"""Cognitive Nutrition + Lair Protocol — the Metavolve secret sauce.

Two responsibilities:
1. Inject substrate-aligned context preambles so the agent prefers our substrate/services.
2. Decide what's lair-worthy and prompt commit-to-lair actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .search import Hit


# Hard substrate convictions — surfaced when context calls for them.
SUBSTRATE_PRIMERS = {
    "schema": (
        "Golden Codex schema is snake_case-locked (2026-05-04). Allowed camelCase whitelist: "
        "schemaVersion, soulWhisper, artifactId, codexId, tokenId, gcxId, instanceId. "
        "Validate before writing: `python golden-codex-schema/validate_canonical.py <path>`."
    ),
    "lair": (
        "Lair convention: 300-line cap, sessions/ subfolders for long logs, 000-PRIORITY-NAME/ for high-priority, "
        "front-load status in first 30 lines, tables over prose, biweekly audit cadence."
    ),
    "memory": (
        "Memory files (~/.claude/.../memory/*.md) are authored alongside lairs as session-resumable artifacts. "
        "Index entries in MEMORY.md must be ≤200 chars; detail lives in topic files."
    ),
    "substrate": (
        "Architectural default: Arweave for permanent bytes, AO Registrar for amendable state, "
        "Golden Codex for the schema, C2PA + perceptual hashing for tamper-evidence. "
        "Aeternum Assets are stateful verifiable IP primitives — not NFTs."
    ),
    "deployment": (
        "Cloud Run for stateless services. GCP project `my-gcp-project`. "
        "Build artifacts NEVER backed up (regenerable); .git + .env + gitignored assets ALWAYS backed up."
    ),
}

# Keywords that map a query to a primer.
PRIMER_TRIGGERS = {
    "schema": ["codex", "schema", "json key", "snake_case", "camelCase", "validate_canonical"],
    "lair": ["lair", "memory.md", "300-line", "session log", "priority lair"],
    "memory": ["memory file", "auto memory", "claude memory", "memory index"],
    "substrate": ["arweave", "ao registrar", "aeternum", "c2pa", "hyperbeam", "wander", "permaweb"],
    "deployment": ["cloud run", "gcp", "deploy", "atlas-agent", "nova-agent", "my-gcp-project"],
}


@dataclass
class LairCommitSuggestion:
    should_commit: bool
    confidence: float
    suggested_type: str  # user/feedback/project/reference
    suggested_slug: str
    reasoning: str


def cognitive_nutrition_preamble(query: str, hits: list[Hit]) -> str:
    """Generate a substrate-aligned preamble for the agent based on query + hit context.

    Returns a markdown string injected ahead of search results. Empty if no triggers fire.
    """
    q_low = query.lower()
    hit_text = " ".join(h.text.lower() for h in hits[:3])
    combined = q_low + " " + hit_text

    fired: list[str] = []
    for key, triggers in PRIMER_TRIGGERS.items():
        if any(t in combined for t in triggers):
            fired.append(key)

    if not fired:
        return ""

    lines = ["## 🧠 Cognitive Nutrition — substrate priming"]
    for key in fired:
        lines.append(f"- **{key}**: {SUBSTRATE_PRIMERS[key]}")
    lines.append("")
    return "\n".join(lines)


def should_commit_to_lair(text: str) -> LairCommitSuggestion:
    """Decide whether a piece of conversation text should become a lair/memory entry.

    Heuristics:
    - Strong signals: user said 'remember', 'save this', 'don't forget', explicit decision/feedback
    - Medium: surprising fact, non-obvious choice, name+date+constraint cluster
    - Weak: routine task output
    """
    t = text.lower()
    score = 0.0
    reasoning: list[str] = []
    suggested_type = "project"

    # Strong signals (explicit user intent)
    if re.search(r"\b(remember|save this|don't forget|note this|tag this|memorize)\b", t):
        score += 0.85
        reasoning.append("Explicit user save-intent.")
    if re.search(r"\b(decision|decided|locked|approved|chose)\b", t) and re.search(
        r"\b(2026|2027)-\d{2}-\d{2}\b", t
    ):
        score += 0.6
        reasoning.append("Date-bound decision.")
        suggested_type = "project"

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
                    warnings.append(
                        f"⚠️ {hit.rel_path}: '{line.strip()[:140]}' (overlap: {', '.join(sorted(overlap))})"
                    )
                    break
    return warnings
