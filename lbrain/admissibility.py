"""Admissibility gate — rung 1 (deterministic, serve-path-safe, no LLM).

Judges whether a retrieved record is EVIDENTIALLY SUFFICIENT for a specific
query, not merely topically relevant. Born from the measured near-domain
inversion (NMPFP pilot -> v3 confirmatory: authentic-but-insufficient records
drive failure-to-abstain 25-72% across model families; relevance-ranked
retrieval serves exactly those records).

Verdicts:
  ADMISSIBLE        — record binds a typed answer candidate to THIS query's
                      specific terms; safe to serve as grounding.
  INADMISSIBLE_NEAR — the hazard class: strong domain overlap, but the query's
                      distinctive specifics are absent or no typed candidate
                      binds to them. Serving this record measurably INDUCES
                      misattribution. Withhold or flag; trigger re-query.
  IRRELEVANT        — low overlap; standard no-hit handling.

Design: three deterministic signals, no model in the path —
  1. answer-type detection from the interrogative (quantity/date/identity/etc.)
  2. specific-vs-generic term split of the query (ids, digit-bearing tokens,
     rare long tokens = specific; the rest = generic domain vocabulary)
  3. sentence-level BINDING: a typed candidate counts only if its sentence
     also carries the query's specific terms.
The signature failure this catches: a record with the right *shape* of answer
bound to the *wrong* entity (the "924 for 780" trap).

v0 scope: English interrogatives, plain-text records. Roadmap (rung 2):
embedding/NLI second stage for paraphrase binding; per-record provenance
metadata binding (source/entity/date match).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

STOP = {
    "the", "a", "an", "is", "was", "were", "are", "did", "does", "do", "of",
    "in", "on", "at", "to", "for", "with", "and", "or", "by", "from", "as",
    "it", "its", "this", "that", "what", "which", "who", "when", "where",
    "how", "many", "much", "about", "roughly", "per", "after", "before",
}

QUANTITY_RE = re.compile(
    r"^(about |roughly )?how (many|much|large|long|fast|complete|old|big)\b", re.I)
DATE_RE = re.compile(r"^when\b|what date|which (day|month|year)|\bdue\b", re.I)

NUM_CAND = re.compile(r"[~$€£]?\d[\d,./x-]*\d%?|\b\d+%?(?![\w-])")
DATE_CAND = re.compile(
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\b[ \d,]*", re.I)
ID_CAND = re.compile(
    r"\b[\w-]*\d[\w./-]*\b"          # digit-bearing ids/versions
    r"|\b[A-Z][\w]*(?:[ -][A-Z][\w]*)+\b"  # Capitalized multiword
    r"|\b\w+_\w+[\w_]*\b"             # snake_case identifiers
    r"|\b\w+\([^)]{0,40}\)"           # call-like tokens f(x)
    r"|\b[a-z]+-[a-z]+(?:-[a-z]+)*\b")  # kebab-case terms


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9$%/.#-]+", " ", s.lower()).strip()


def _terms(s: str) -> list[str]:
    out = []
    for t in _norm(s).split():
        if not t or t in STOP:
            continue
        out.append(t)
        if "-" in t:                      # hyphen variants match both forms
            out.extend(p for p in t.split("-") if len(p) >= 3 and p not in STOP)
    return out


def _is_specific(tok: str) -> bool:
    """High-information query tokens: ids, digit-bearing, hyphenated, long-rare."""
    return bool(re.search(r"\d", tok)) or "-" in tok or "/" in tok or len(tok) >= 8


def qtype(question: str) -> str:
    if QUANTITY_RE.search(question.strip()):
        return "quantity"
    if DATE_RE.search(question.strip()):
        return "date"
    return "identity"


def _candidates(sentence: str, kind: str) -> list[str]:
    if kind == "quantity":
        return NUM_CAND.findall(sentence)
    if kind == "date":
        return [m if isinstance(m, str) else m[0] for m in DATE_CAND.findall(sentence)] \
            + NUM_CAND.findall(sentence)
    return ID_CAND.findall(sentence)


@dataclass
class Verdict:
    verdict: str                    # ADMISSIBLE | INADMISSIBLE_NEAR | IRRELEVANT
    qkind: str
    specific_coverage: float        # query specifics found in record
    generic_overlap: float          # domain vocabulary overlap
    bound_candidates: list[str] = field(default_factory=list)
    reason: str = ""


def judge(question: str, record: str,
          near_threshold: float = 0.28,
          specific_threshold: float = 0.5) -> Verdict:
    kind = qtype(question)
    q_terms = _terms(question)
    # proper nouns in the raw question (non-initial capitalized tokens) are
    # entity anchors — always specific, regardless of length
    proper = {_norm(w) for w in re.findall(r"(?<!^)(?<![.?!] )\b[A-Z][\w-]*", question)}
    proper = {p for p in proper if p and p not in STOP}
    specifics = [t for t in q_terms if _is_specific(t) or t in proper]
    generics = [t for t in q_terms if t not in specifics]
    rec_norm = _norm(record)
    rec_tokens = set(rec_norm.split())

    rec_flat = rec_norm.replace("-", " ")

    def hit(t: str) -> bool:
        if t in rec_tokens or (len(t) >= 6 and t in rec_norm):
            return True
        tt = t.replace("-", " ")
        if len(tt) >= 6 and tt in rec_flat:
            return True
        # morphology-lite: shared root for longer tokens (fallback ~ falls back)
        root = re.sub(r"(ing|back|ed|es|s)$", "", t)
        return len(root) >= 5 and root in rec_flat

    spec_cov = (sum(hit(t) for t in specifics) / len(specifics)) if specifics else 1.0
    gen_ov = (sum(hit(t) for t in generics) / len(generics)) if generics else 0.0

    # binding pass: typed candidate in a sentence that carries query terms,
    # weighted toward specifics (a candidate bound only to generic vocabulary
    # is the misattribution trap, not evidence).
    bound: list[str] = []
    sents = re.split(r"(?<=[.;!?])\s+", record)
    for i in range(len(sents)):
        win = " ".join(sents[max(0, i - 1):i + 2])   # 3-sentence binding window
        w_terms = set(_terms(win))
        spec_here = sum(1 for t in specifics if t in w_terms or
                        (len(t) >= 6 and t in _norm(win)))
        gen_here = sum(1 for t in generics if t in w_terms)
        anchored = (spec_here >= 1 and (spec_here + gen_here) >= 2) if specifics \
            else (gen_here >= max(2, len(generics) // 2))
        if anchored:
            bound.extend(_candidates(sents[i], kind))

    relevance = max(gen_ov, spec_cov)
    # entity-anchor rule: when the query names a proper entity, ADMISSIBLE
    # requires that anchor to appear — a sibling record matching only the
    # generic specifics is exactly the misattribution trap.
    anchor_ok = (not proper) or any(hit(p) for p in proper)
    if bound and anchor_ok and (spec_cov >= specific_threshold or not specifics):
        return Verdict("ADMISSIBLE", kind, spec_cov, gen_ov, bound,
                       "typed candidate bound to query specifics")
    if relevance >= near_threshold:
        why = ("no typed candidate bound to query specifics"
               if spec_cov >= specific_threshold else
               "domain overlap without the query's distinctive specifics")
        return Verdict("INADMISSIBLE_NEAR", kind, spec_cov, gen_ov, bound, why)
    return Verdict("IRRELEVANT", kind, spec_cov, gen_ov, [], "low overlap")
