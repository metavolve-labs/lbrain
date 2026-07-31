"""Disclosure control — the permissions layer (Agent-X LAIR §6.2) and the three
blinding operations (§3), enforced at the engine rather than described in a doc.

WHY THIS EXISTS
---------------
§3 says the whole architecture turns on one thing:

    Five personas reading the same corpus with the same filters produce five
    paraphrases of one opinion. Differentiation must live in *what each agent is
    permitted to see*, not only in its prompt.

and names the failure it becomes otherwise: **"Claude with a funny accent."**
§6.2 records that `permissions` was named in the canon and implemented nowhere.
Until this module, "independent review" was a naming convention — the reviewer
received the author's framing along with the artifact and produced a second
opinion that was not second.

TWO CONTROLS, DELIBERATELY SEPARATE
-----------------------------------
- **Permissions** — a STANDING scope. Which paths and doc types a persona may
  ever read. Provisioned in that persona's config; closes A-428, whose finding
  was that `doc_type`/`priority_only` existed only as call-time parameters the
  calling model chose for itself — i.e. advisory, i.e. unfalsifiable.
- **Disclosure mode** — a PER-REQUEST blinding envelope. The same agent reviews
  independently on Monday and collaboratively on Tuesday, so this cannot be a
  property of the persona.

THE RULE THAT RECONCILES "MODE IS A REQUEST PROPERTY" WITH "A CONTROL A MODEL
CAN SET IS NOT A CONTROL"
-----------------------------------------------------------------------------
    The environment sets a CEILING. A request may only NARROW it, never widen it.

The router launches an adversarial review with `LBRAIN_DISCLOSURE=adversarial`
and the model cannot climb out of it by asking. Inside an unrestricted session an
agent may still voluntarily blind itself for a sub-task. Narrowing is always
safe; widening is impossible. Same reasoning as `LBRAIN_PERSONA` being read from
the environment instead of accepted as a tool argument.

FAIL CLOSED, AND WHAT THAT MEANS HERE
-------------------------------------
- `LBRAIN_DISCLOSURE` **unset** → `full`. No ceiling was *requested*; this is the
  pre-existing behaviour and a deployment decision, not a code default.
- `LBRAIN_DISCLOSURE` **set to anything outside the vocabulary** — empty, wrong
  case, a glob, a traversal, an injection string → `adversarial` with an EMPTY
  seal, which admits nothing at all, plus a loud warning. Someone tried to
  configure a control and got it wrong; failing open there hands the whole corpus
  to an agent that was meant to be blinded.
- A malformed token anywhere in the seal invalidates the **entire** seal. A
  partially-parsed whitelist is a silent downgrade, which is worse than a refusal
  because it still returns plausible results.

AND THE PART THAT IS EASY TO GET WRONG
--------------------------------------
Withholding must be **reported**, loudly, in the served response. A blinded
reviewer has to know it is blinded — it simply cannot see through. An agent
handed a silently-thinned corpus does not conclude "I am missing context", it
answers confidently from what remains. That is the A-430 shape (a fail-open that
review passed) pointed the other way, and it is why `Withheld` travels back with
the hits instead of being discarded.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

# --- modes, ordered RESTRICTIVE → PERMISSIVE. The order is the semantics: -----
MODE_ADVERSARIAL = "adversarial"     # only the sealed artifact. no framing, no intent
MODE_INDEPENDENT = "independent"     # artifacts only — a second opinion that is actually second
MODE_COLLABORATIVE = "collaborative"  # artifacts + the proposal — build on work in progress
MODE_FULL = "full"                   # no blinding requested
MODES = (MODE_ADVERSARIAL, MODE_INDEPENDENT, MODE_COLLABORATIVE, MODE_FULL)
_RANK = {m: i for i, m in enumerate(MODES)}

# --- disclosure classes ------------------------------------------------------
CLASS_ARTIFACT = "artifact"      # a durable, sealed record. the output
CLASS_PROPOSAL = "proposal"      # work in progress: framing, intent, plans
CLASS_PRIVATE = "private"        # one persona's working memory
CLASSES = (CLASS_ARTIFACT, CLASS_PROPOSAL, CLASS_PRIVATE)
UNCLASSIFIED = ""

# What each mode ADMITS. `full` admits everything (None = no class filter);
# `adversarial` admits nothing by class — it is a whitelist, not a filter.
ADMITS: dict[str, frozenset[str] | None] = {
    MODE_FULL: None,
    MODE_COLLABORATIVE: frozenset({CLASS_ARTIFACT, CLASS_PROPOSAL}),
    MODE_INDEPENDENT: frozenset({CLASS_ARTIFACT}),
    MODE_ADVERSARIAL: frozenset(),
}

# A seal token is a bare slug and nothing else. Anything carrying a separator, a
# traversal, a wildcard or a quote is rejected — not sanitized. Sanitizing
# `../cto` into `cto` would silently honour a malformed whitelist, and a
# whitelist is precisely where silent normalization must not happen.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

WITHHELD_NOTICE = (
    "⚠ {n} record(s) WITHHELD by disclosure mode '{mode}' ({detail}).\n"
    "  You are reading a deliberately blinded view. Do not treat what remains as\n"
    "  the whole record.{hint}"
)
_CLASSIFY_HINT = (
    "\n  Nothing in this corpus is classified yet: add `disclosure: artifact|proposal|private`\n"
    "  frontmatter, or set `disclosure_default` in config.toml."
)


@dataclass
class Withheld:
    """What the envelope removed, and why. Counts only — never the content."""

    by_class: dict[str, int] = field(default_factory=dict)
    by_permission: int = 0
    total: int = 0

    def note(self, reason: str) -> None:
        self.by_class[reason] = self.by_class.get(reason, 0) + 1
        self.total += 1

    def detail(self) -> str:
        parts = [f"{n} {k}" for k, n in sorted(self.by_class.items(), key=lambda kv: -kv[1])]
        if self.by_permission:
            parts.append(f"{self.by_permission} out-of-scope")
        return ", ".join(parts) or "0"

    def notice(self, mode: str) -> str:
        if not self.total:
            return ""
        unclassified = self.by_class.get("unclassified", 0)
        return WITHHELD_NOTICE.format(
            n=self.total, mode=mode, detail=self.detail(),
            hint=_CLASSIFY_HINT if unclassified and unclassified == self.total else "",
        )


@dataclass(frozen=True)
class Envelope:
    """The effective disclosure envelope for ONE request."""

    mode: str = MODE_FULL
    sealed: frozenset[str] | None = None       # None = no seal declared
    allowed_doc_types: frozenset[str] = frozenset()   # empty = no restriction
    allowed_path_prefixes: tuple[str, ...] = ()       # empty = no restriction
    force_priority_only: bool = False
    default_class: str = UNCLASSIFIED
    warnings: tuple[str, ...] = ()

    @property
    def blinding(self) -> bool:
        return self.mode != MODE_FULL

    @property
    def admits(self) -> frozenset[str] | None:
        return ADMITS[self.mode]


def _norm_path(p: str) -> str:
    """Corpus paths compare on forward slashes, always.

    On Windows a rel_path is `P3-NEURAINETIC-BRAIN\\x\\LAIR.md`, so a prefix
    written the obvious way (`P3-NEURAINETIC-BRAIN/`) matched nothing and the
    scope silently admitted everything. That exact separator bug has now shipped
    twice in this codebase (A-404 in search, and the 000-PRIORITY boost before
    it) — both times as a behaviour difference with no error message.
    """
    return str(p).replace("\\", "/")


def parse_seal(raw: str | None) -> tuple[frozenset[str] | None, list[str]]:
    """Parse `LBRAIN_SEALED`. Returns (seal, warnings); seal None = not declared.

    ONE malformed token empties the WHOLE seal. Dropping just the bad token would
    hand back a quietly smaller whitelist that still returns plausible records —
    a silent downgrade of a disclosure control, which is the failure this module
    is built to make impossible.
    """
    if raw is None:
        return None, []
    tokens = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    if not tokens:
        # Declared but empty. Honour it literally: seal nothing.
        return frozenset(), []
    bad = [t for t in tokens if not _SLUG_RE.match(t)]
    if bad:
        return frozenset(), [
            f"LBRAIN_SEALED contains {len(bad)} malformed token(s) "
            f"({', '.join(repr(b) for b in bad[:3])}) — the ENTIRE seal is void. "
            "Nothing will be disclosed."
        ]
    return frozenset(tokens), []


def _mode_or_closed(raw: str, source: str) -> tuple[str, bool, list[str]]:
    """(mode, ok, warnings). An unrecognised value closes rather than opens."""
    v = (raw or "").strip().lower()
    if v in _RANK:
        return v, True, []
    return MODE_ADVERSARIAL, False, [
        f"{source}={raw!r} is not one of {'/'.join(MODES)} — failing CLOSED to "
        f"'{MODE_ADVERSARIAL}' with an empty seal. Nothing will be disclosed."
    ]


def resolve(
    cfg,
    env: dict | None = None,
    *,
    requested_mode: str | None = None,
    requested_seal: str | None = None,
    warn: bool = True,
) -> Envelope:
    """Compute the effective envelope: environment ceiling ∧ request.

    `cfg` supplies the STANDING permissions (provisioned per persona, not
    chooseable by the model); the environment supplies the ceiling; the request
    may narrow. Nothing here can widen anything.
    """
    env = os.environ if env is None else env
    warnings: list[str] = []

    # --- ceiling from the environment ---
    raw_mode = env.get("LBRAIN_DISCLOSURE")
    if raw_mode is None:
        ceiling = MODE_FULL
        ceiling_ok = True
    else:
        ceiling, ceiling_ok, w = _mode_or_closed(raw_mode, "LBRAIN_DISCLOSURE")
        warnings += w

    ceiling_seal, w = parse_seal(env.get("LBRAIN_SEALED"))
    warnings += w
    if not ceiling_ok:
        # A malformed mode voids any seal that accompanied it. Honouring the seal
        # of a request whose mode we could not understand would disclose exactly
        # the records someone fumbled the configuration around.
        ceiling_seal = frozenset()

    # --- the request may only narrow ---
    mode = ceiling
    if requested_mode is not None:
        req, req_ok, w = _mode_or_closed(requested_mode, "requested disclosure mode")
        warnings += w
        if not req_ok:
            ceiling_seal = frozenset()
        mode = req if _RANK[req] < _RANK[ceiling] else ceiling

    seal = ceiling_seal
    if requested_seal is not None:
        req_seal, w = parse_seal(requested_seal)
        warnings += w
        if req_seal is not None:
            seal = req_seal if ceiling_seal is None else (ceiling_seal & req_seal)

    if warnings and warn:
        for line in warnings:
            print(f"[lbrain] DISCLOSURE: {line}", file=sys.stderr)

    default_class = str(getattr(cfg, "disclosure_default", "") or "").strip().lower()
    if default_class and default_class not in CLASSES:
        msg = (
            f"disclosure_default={default_class!r} is not one of {'/'.join(CLASSES)} — "
            "treating every unclassified document as UNCLASSIFIED (withheld under any "
            "blinding mode)."
        )
        warnings.append(msg)
        if warn:
            print(f"[lbrain] DISCLOSURE: {msg}", file=sys.stderr)
        default_class = UNCLASSIFIED

    return Envelope(
        mode=mode,
        sealed=seal,
        allowed_doc_types=frozenset(
            str(x).strip() for x in (getattr(cfg, "allowed_doc_types", []) or []) if str(x).strip()
        ),
        allowed_path_prefixes=tuple(
            _norm_path(x).strip() for x in (getattr(cfg, "allowed_path_prefixes", []) or [])
            if str(x).strip()
        ),
        force_priority_only=bool(getattr(cfg, "force_priority_only", False)),
        default_class=default_class,
        warnings=tuple(warnings),
    )


def classify(raw_class: str, belief_state: str | None, default_class: str) -> str:
    """The disclosure class of one document. Precedence is explicit → derived →
    configured default → unclassified.

    A belief's lifecycle already answers this question, so it is not asked twice:
    a draft is one agent's working memory (`private`); anything promoted or
    withdrawn is a durable record of what this agent concluded (`artifact`).
    """
    v = (raw_class or "").strip().lower()
    if v in CLASSES:
        return v
    if belief_state:
        return CLASS_PRIVATE if belief_state == "draft" else CLASS_ARTIFACT
    return default_class


def permitted(rel_path: str, doc_type: str, is_priority: bool, env: Envelope) -> bool:
    """Standing scope: may this persona read this document AT ALL? (A-428.)

    Applies in every mode, including `full` — permissions are standing, blinding
    is per-request, and conflating them would make scope evaporate the moment a
    request stopped asking to be blinded.
    """
    if env.allowed_doc_types and doc_type not in env.allowed_doc_types:
        return False
    if env.force_priority_only and not is_priority:
        return False
    if env.allowed_path_prefixes:
        p = _norm_path(rel_path)
        if not any(p.startswith(pref) for pref in env.allowed_path_prefixes):
            return False
    return True


def narrow_doc_type(requested: str | None, env: Envelope) -> tuple[str | None, bool]:
    """Intersect a caller's `doc_type` with the standing allowlist.

    Returns (effective, ok). `ok=False` means the caller asked for a type outside
    its scope — the answer is NOTHING, not everything. A filter that silently
    widens when it cannot be satisfied is how "scope" becomes decorative.
    """
    if not env.allowed_doc_types:
        return requested, True
    if requested is None:
        return None, True  # unrestricted request → the allowlist does the work
    return (requested, True) if requested in env.allowed_doc_types else (requested, False)


def apply(
    hits: list,
    classes: dict[str, str],
    belief_states: dict[str, tuple[str, str]],
    env: Envelope,
    *,
    slug_of=None,
) -> tuple[list, Withheld]:
    """Filter hits through the envelope. Returns (kept, withheld).

    Order matters: standing PERMISSIONS first, then the per-request blinding.
    A record outside a persona's scope is not "withheld by the mode" — it was
    never in scope — and reporting them separately keeps the notice honest about
    which control fired.
    """
    from .search import _basename_slug

    slug_of = slug_of or _basename_slug
    w = Withheld()
    if not hits:
        return list(hits), w

    kept = []
    for h in hits:
        if not permitted(h.rel_path, h.doc_type, h.is_priority, env):
            w.by_permission += 1
            w.total += 1
            continue

        if env.mode == MODE_FULL:
            kept.append(h)
            continue

        if env.mode == MODE_ADVERSARIAL:
            # A whitelist, not a filter: the sealed artifact and nothing else.
            if env.sealed and slug_of(h.rel_path) in env.sealed:
                h.boosts["sealed"] = 1.0
                kept.append(h)
            else:
                w.note("not sealed")
            continue

        cls = classify(classes.get(h.rel_path, ""), (belief_states.get(h.rel_path) or (None, None))[1],
                       env.default_class)
        admitted = env.admits
        if admitted is not None and cls not in admitted:
            w.note(cls if cls else "unclassified")
            continue
        h.boosts[f"disclosed:{cls or 'unclassified'}"] = 1.0
        kept.append(h)

    return kept, w
