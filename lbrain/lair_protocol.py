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
from pathlib import Path
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


# Words that MARK a line as directive. Both polarities: the old detector recognised
# prohibitions only, so positively phrased guidance — "verify before asserting",
# "always check the live source" — was structurally invisible, and most real guidance
# is phrased that way. Measured 2026-08-01: 1/8 on realistic actions (A-438).
_PROHIBITIVE = ("don't", "dont", "do not", "never", "avoid", "stop ", "no longer",
                "must not", "cannot", "rather than", "instead of")
_PRESCRIPTIVE = ("always", "before ", "must ", "first ", "ensure", "verify", "prefer",
                 "say instead", "retired", "use ", "only ", "then ")

# Suffixes stripped for morphology. `recoverable` != `unrecoverable` and `test` !=
# `tests` were both misses under exact-set matching.
_SUFFIXES = ("ations", "ation", "ities", "ility", "ingly", "ables", "able", "ible",
             "ings", "ing", "ers", "er", "ed", "es", "s", "ly")


def _stem(w: str) -> str:
    for suf in _SUFFIXES:
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


# Function words that survived the original list and actively hurt: they matched
# across unrelated sentences and padded the denominator, pushing real matches under
# threshold. `the` matching `the` is not evidence of anything.
_NOISE = frozenset("""
the and but for are was has had can may will did you our its out not from into one
any all use way get let put via per own new old off yet its it's than then them
""".split())


def _tokens(text: str) -> set[str]:
    """Content tokens, stemmed, hyphen-split. Minimum 3 chars, not 4 — the old floor
    made `key`, `api` and `27x` invisible, and those carry the meaning in a short
    action description. Hyphenated forms yield BOTH the compound and its parts, so
    `fan-out` in a rule meets `fan out` in an action."""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9][a-z0-9\-.]{2,}", text.lower()):
        w = w.strip("-.")
        for piece in [w, *w.replace(".", "-").split("-")]:
            if len(piece) >= 3 and piece not in _STOPWORDS and piece not in _NOISE:
                out.add(_stem(piece))
    return out - _STOPWORDS - _NOISE


def _related(a: str, b: str) -> bool:
    """One token is evidence for another if either contains the other (>=5 chars).

    Cheap, and it is what makes `unrecoverable` match `recoverable` — a negation
    prefix is the single most common way our rules and our actions diverge in
    spelling while meaning the same thing.
    """
    if a == b:
        return True
    return len(a) >= 5 and len(b) >= 5 and (a in b or b in a)


def _overlap(action_toks: set[str], rule_toks: set[str]) -> set[str]:
    return {a for a in action_toks if any(_related(a, r) for r in rule_toks)}


def _idf(feedback_hits: list) -> dict:
    """Inverse document frequency over the supplied rules.

    Without it every token counts the same, and that was the remaining half of
    A-438: a single match on `recoverable` — which is nearly conclusive — was
    discarded by a >=2 floor, while `the` and `out` padded other actions to exactly
    2 and then diluted the denominator below threshold. Specificity IS the signal.
    """
    import math

    n = max(len(feedback_hits), 1)
    df: dict[str, int] = {}
    for h in feedback_hits:
        for t in _tokens(getattr(h, "text", "")):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _weight(toks: set[str], idf: dict) -> float:
    # Unseen tokens are maximally specific: absent from every rule means nothing
    # common about them.
    default = max(idf.values(), default=1.0)
    return sum(idf.get(t, default) for t in toks)


class _CoreRule:
    """Core memory as a rule source. Operator-curated and already injected into every
    query, so it adds no trust surface — but it was invisible to check-action, which
    filters to doc_type == "feedback".

    That was the deepest layer of A-438. Measured 2026-08-01: "describe our research
    as peer-reviewed" returned ZERO feedback hits, because the never-say list lives
    in CORE.md and in a lair, not in a `type: feedback` document. The matcher was
    being blamed for a corpus the tool could not see. Our standing guidance is spread
    across four document classes and this searched one.
    """

    doc_type = "feedback"

    def __init__(self, text: str):
        self.text = text
        self.rel_path = "CORE.md (always-on doctrine)"
        self.title = "core memory"


def core_rules(core_memory_path: str) -> list:
    """Zero or one pseudo-hit carrying the operator's always-on doctrine."""
    if not core_memory_path:
        return []
    p = Path(core_memory_path).expanduser()
    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [_CoreRule(body)] if body.strip() else []


def detect_anti_pattern(action_description: str, feedback_hits: list[Hit]) -> list[str]:
    """Surface saved guidance RELEVANT to a proposed action.

    Deliberately not "detect a violation". Telling compliance from violation needs
    polarity reasoning a bag of words cannot do honestly, and a tool that claims
    adjudication it cannot perform is worse than one that surfaces evidence and says
    so. The caller is told to weigh these, never to obey them.

    Scoring is a NORMALISED overlap, not a raw count of >=3: a raw floor punished
    short action descriptions, which is most of them, and was half of why this fired
    on 1 of 8 realistic actions (A-438).
    """
    from .serve import sanitize_field

    action_toks = _tokens(action_description)
    if not action_toks:
        return []
    idf = _idf(feedback_hits)

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for hit in feedback_hits:
        if getattr(hit, "doc_type", "") != "feedback":
            continue
        for line in hit.text.split("\n"):
            stripped = line.strip()
            if len(stripped) < 12:
                continue
            ll = stripped.lower()
            if not (any(m in ll for m in _PROHIBITIVE) or any(m in ll for m in _PRESCRIPTIVE)):
                continue
            rule_toks = _tokens(stripped)
            if not rule_toks:
                continue
            matched = _overlap(action_toks, rule_toks)
            if not matched:
                continue
            # Weighted by specificity, and normalised by the smaller side so a long
            # rule cannot dilute a precise short action, nor a long action light up
            # on one incidental word.
            mw = _weight(matched, idf)
            denom = min(_weight(action_toks, idf), _weight(rule_toks, idf))
            score = mw / denom if denom else 0.0
            if score < 0.30:
                continue
            key = f"{hit.rel_path}:{stripped[:60]}"
            if key in seen:
                continue
            seen.add(key)
            scored.append((
                score,
                # Both fields are corpus-derived. Unsanitized, a note body carrying
                # \r / U+2028 / a homoglyph fence forges a second line at column 0
                # inside the caller's output (red-team 2026-07-28, finding 1).
                f"⚠️ {sanitize_field(hit.rel_path, 160)}: "
                f"'{sanitize_field(stripped, 160)}' "
                f"(matched: {', '.join(sorted(matched))})",
            ))

    scored.sort(key=lambda t: -t[0])
    return [w for _, w in scored[:5]]
