"""Disclosure control — the permissions layer (§6.2) and the three blinding
operations (§3), enforced at the engine.

Every test here was MUTATION-TESTED: the behaviour was deliberately broken and
the test confirmed to fail before being kept.

THE NEGATIVE-CONTROL RULE, applied throughout
---------------------------------------------
"The hostile value returned nothing" is worthless on its own — a query that
matches nothing returns nothing too, and every such assertion would pass while
proving nothing. So each fail-closed test also asserts that a record which SHOULD
be visible IS, in the same call. Adopted from the router lane's adversarial suite,
which found the same trap on the draft-isolation side.
"""

import tempfile
from pathlib import Path

import pytest

from lbrain import disclosure as D
from lbrain.index import parse
from lbrain.search import Hit, HitList, apply_disclosure, keyword_only, search
from lbrain.store import Store


class _Cfg:
    """Minimal config stand-in — the envelope only reads these four."""

    def __init__(self, **kw):
        self.disclosure_default = kw.get("disclosure_default", "")
        self.allowed_doc_types = kw.get("allowed_doc_types", [])
        self.allowed_path_prefixes = kw.get("allowed_path_prefixes", [])
        self.force_priority_only = kw.get("force_priority_only", False)


def _hit(rel_path, doc_type="", is_priority=False):
    return Hit(rel_path=rel_path, chunk_idx=0, text="t", title=rel_path, score=1.0,
               doc_type=doc_type, is_priority=is_priority)


def _apply(hits, env, classes=None, beliefs=None):
    return D.apply(hits, classes or {}, beliefs or {}, env)


# ==========================================================================
# the ceiling / narrowing rule
# ==========================================================================


def test_unset_ceiling_means_no_blinding():
    """Pre-existing behaviour. Not requesting a control differs from requesting a
    broken one, and conflating them would break every existing call."""
    env = D.resolve(_Cfg(), env={}, warn=False)
    assert env.mode == D.MODE_FULL and not env.blinding


def test_a_request_may_narrow_the_ceiling():
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "collaborative"},
                    requested_mode="independent", warn=False)
    assert env.mode == D.MODE_INDEPENDENT


def test_a_request_may_NOT_widen_the_ceiling():
    """The whole reason a mode can be a request property without becoming a
    control a model lifts by asking."""
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"},
                    requested_mode="full", warn=False)
    assert env.mode == D.MODE_INDEPENDENT, "asking for 'full' must not grant it"

    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": "a"},
                    requested_mode="collaborative", warn=False)
    assert env.mode == D.MODE_ADVERSARIAL


def test_a_request_may_not_widen_the_seal():
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": "a"},
                    requested_seal="a,b,c", warn=False)
    assert env.sealed == frozenset({"a"}), "the seal intersects; it never unions"


def test_an_unblinded_session_may_still_blind_itself():
    """The same agent, independently on Monday and collaboratively on Tuesday."""
    env = D.resolve(_Cfg(), env={}, requested_mode="adversarial", requested_seal="paper-b",
                    warn=False)
    assert env.mode == D.MODE_ADVERSARIAL and env.sealed == frozenset({"paper-b"})


# ==========================================================================
# FAIL CLOSED — every hostile value, each with a negative control
# ==========================================================================


HOSTILE_MODES = [
    "", " ", "independant", "*", "?",
    "../full", "..", "/full", "full;independent", "full' OR '1'='1",
    "${LBRAIN_DISCLOSURE}", "None", "null", "0", "-1", "true",
    "independent\nfull", "independent\x00full", "🙂", "independent full",
    "FULL!", "in dependent",
]


@pytest.mark.parametrize("ok", ["Independent", "INDEPENDENT", "full ", " collaborative\t"])
def test_mode_case_and_whitespace_are_normalised_deliberately(ok):
    """Not an oversight, and worth pinning as distinct from the persona rule.

    A MODE comes from a fixed, closed vocabulary, so folding case resolves no
    ambiguity — 'INDEPENDENT' can only mean the one thing. A PERSONA is an
    identity in an open space, where folding case would invent an equivalence
    nobody declared, so there it warns instead (see check_persona). Different
    rules for different kinds of string, on purpose.
    """
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": ok}, warn=False)
    assert env.mode == ok.strip().lower()


@pytest.mark.parametrize("bad", HOSTILE_MODES)
def test_a_malformed_ceiling_fails_closed_to_nothing(bad):
    """Unset means 'no ceiling requested'. SET-but-unparseable means someone tried
    to configure a control and got it wrong — failing open there would hand the
    whole corpus to an agent that was meant to be blinded."""
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": bad}, warn=False)
    assert env.mode == D.MODE_ADVERSARIAL
    assert not env.sealed, "a mode we could not parse must not honour any seal"

    hits = [_hit("a.md"), _hit("b.md")]
    kept, w = _apply(hits, env, classes={"a.md": "artifact", "b.md": "artifact"})
    assert kept == [], f"{bad!r} disclosed something"
    assert w.total == 2

    # NEGATIVE CONTROL — the same hits, the same corpus, a VALID ceiling. If this
    # were empty too, every assertion above would be vacuous.
    ok = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    kept_ok, _ = _apply([_hit("a.md"), _hit("b.md")], ok,
                        classes={"a.md": "artifact", "b.md": "artifact"})
    assert len(kept_ok) == 2, "negative control failed — the test proves nothing"


HOSTILE_SEALS = [
    "../cto", "..", "*", "a/b", "a\\b", "a b/../c", "'; DROP TABLE docs;--",
    "a,../b", "%2e%2e", "a|b", "<script>", "a\x00b", "-a", ".hidden",
]


@pytest.mark.parametrize("bad", HOSTILE_SEALS)
def test_one_malformed_seal_token_voids_the_entire_seal(bad):
    """Dropping only the bad token would hand back a quietly smaller whitelist
    that still returns plausible records — a silent downgrade of a disclosure
    control, which is worse than a refusal."""
    seal, warns = D.parse_seal(bad)
    assert seal == frozenset(), f"{bad!r} produced a live seal"
    assert warns, "a voided seal must say so"

    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": bad},
                    warn=False)
    kept, w = _apply([_hit("cto.md"), _hit("b.md")], env)
    assert kept == [] and w.total == 2

    # NEGATIVE CONTROL — a well-formed seal over the same corpus discloses.
    good = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": "cto"},
                     warn=False)
    kept_ok, _ = _apply([_hit("cto.md"), _hit("b.md")], good)
    assert [h.rel_path for h in kept_ok] == ["cto.md"], "negative control failed"


def test_a_malformed_mode_VOIDS_an_otherwise_valid_seal():
    """The hole my first pass left: every malformed-ceiling case set no seal, so
    the line that voids one was never exercised and a mutation removing it
    survived. With a *valid* seal alongside a *malformed* mode, dropping that line
    discloses exactly the records someone fumbled the configuration around."""
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independant",
                                 "LBRAIN_SEALED": "paper"}, warn=False)
    assert env.mode == D.MODE_ADVERSARIAL
    assert env.sealed == frozenset(), "a mode we could not parse must not honour its seal"
    kept, w = _apply([_hit("paper.md"), _hit("other.md")], env)
    assert kept == [] and w.total == 2

    # NEGATIVE CONTROL — the same seal under a VALID mode does disclose.
    ok = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial",
                                "LBRAIN_SEALED": "paper"}, warn=False)
    kept_ok, _ = _apply([_hit("paper.md"), _hit("other.md")], ok)
    assert [h.rel_path for h in kept_ok] == ["paper.md"], "negative control failed"


def test_a_traversal_seal_is_rejected_not_sanitised():
    """`../cto` must NOT quietly become `cto`. Normalising a whitelist silently
    honours a malformed one, and a whitelist is exactly where that must not
    happen — the operator has to see that their seal was wrong."""
    seal, _ = D.parse_seal("../cto")
    assert seal == frozenset()
    assert "cto" not in (seal or set())


def test_a_declared_but_empty_seal_seals_nothing():
    seal, warns = D.parse_seal("")
    assert seal == frozenset() and not warns
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": ""},
                    warn=False)
    kept, _ = _apply([_hit("a.md")], env)
    assert kept == []


def test_a_malformed_requested_mode_also_fails_closed():
    """Narrowing is safe; an unparseable narrowing request is not a reason to
    fall back to the ceiling."""
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "full"},
                    requested_mode="independant", warn=False)
    assert env.mode == D.MODE_ADVERSARIAL and not env.sealed


def test_a_bad_disclosure_default_does_not_invent_a_class():
    env = D.resolve(_Cfg(disclosure_default="public"), env={}, warn=False)
    assert env.default_class == D.UNCLASSIFIED
    assert env.warnings


# ==========================================================================
# the three blinding operations (§3)
# ==========================================================================


def _corpus():
    return (
        [_hit("paper.md"), _hit("plan.md"), _hit("hunch.md"), _hit("untagged.md")],
        {"paper.md": "artifact", "plan.md": "proposal", "hunch.md": "private"},
    )


def test_independent_receives_artifacts_only():
    """§3: 'a second opinion that is actually second'. The proposal — the framing
    and the intent — must not reach the reviewer, or it is not independent."""
    hits, classes = _corpus()
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    kept, w = _apply(hits, env, classes=classes)
    assert [h.rel_path for h in kept] == ["paper.md"]
    assert w.total == 3 and w.by_class["proposal"] == 1 and w.by_class["private"] == 1


def test_collaborative_receives_artifacts_and_the_proposal():
    hits, classes = _corpus()
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "collaborative"}, warn=False)
    kept, w = _apply(hits, env, classes=classes)
    assert [h.rel_path for h in kept] == ["paper.md", "plan.md"]
    assert w.by_class.get("private") == 1


def test_adversarial_receives_the_sealed_artifact_and_nothing_else():
    """A whitelist, not a filter — 'no framing, no intent'. Even other artifacts
    are withheld."""
    hits, classes = _corpus()
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "adversarial", "LBRAIN_SEALED": "paper"},
                    warn=False)
    kept, w = _apply(hits, env, classes=classes)
    assert [h.rel_path for h in kept] == ["paper.md"]
    assert kept[0].boosts.get("sealed") == 1.0
    assert w.total == 3


def test_full_receives_everything():
    hits, classes = _corpus()
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "full"}, warn=False)
    kept, w = _apply(hits, env, classes=classes)
    assert len(kept) == 4 and w.total == 0


def test_unclassified_is_withheld_under_every_blinding_mode():
    """The decision that makes this enforcement rather than a naming convention.
    A document that cannot be PROVEN to be an artifact is not disclosed to an
    independent reviewer — otherwise an unclassified proposal contaminates the
    review, which is precisely A-428."""
    for mode in (D.MODE_INDEPENDENT, D.MODE_COLLABORATIVE):
        env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": mode}, warn=False)
        kept, w = _apply([_hit("untagged.md")], env, classes={})
        assert kept == [], mode
        assert w.by_class["unclassified"] == 1


def test_a_configured_default_makes_an_unclassified_corpus_usable():
    env = D.resolve(_Cfg(disclosure_default="artifact"),
                    env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    kept, w = _apply([_hit("untagged.md")], env, classes={})
    assert len(kept) == 1 and w.total == 0


def test_belief_state_classifies_without_frontmatter():
    """A belief's lifecycle already answers 'is this durable or working memory?',
    so the question is not asked twice."""
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = [_hit("d.md"), _hit("p.md")]
    beliefs = {"d.md": ("cfo", "draft"), "p.md": ("cfo", "promoted")}
    kept, w = _apply(hits, env, classes={}, beliefs=beliefs)
    assert [h.rel_path for h in kept] == ["p.md"]
    assert w.by_class["private"] == 1


def test_explicit_frontmatter_beats_the_derived_belief_class():
    assert D.classify("proposal", "promoted", "") == "proposal"
    assert D.classify("", "promoted", "") == "artifact"
    assert D.classify("", "draft", "artifact") == "private"


# ==========================================================================
# standing permissions (closes A-428)
# ==========================================================================


def test_a_doc_type_outside_the_allowlist_is_withheld():
    env = D.resolve(_Cfg(allowed_doc_types=["project"]), env={}, warn=False)
    kept, w = _apply([_hit("a.md", doc_type="project"), _hit("b.md", doc_type="user")], env)
    assert [h.rel_path for h in kept] == ["a.md"]
    assert w.by_permission == 1


def test_asking_for_a_type_outside_scope_returns_NOTHING_not_everything():
    """A filter that widens when it cannot be satisfied is how scope becomes
    decorative — the exact finding in A-428."""
    env = D.resolve(_Cfg(allowed_doc_types=["project"]), env={}, warn=False)
    eff, ok = D.narrow_doc_type("user", env)
    assert ok is False

    # NEGATIVE CONTROL — an in-scope request is honoured.
    eff2, ok2 = D.narrow_doc_type("project", env)
    assert ok2 is True and eff2 == "project"


def test_path_scope_matches_on_both_separators():
    """A rel_path is backslash-separated on Windows, so a forward-slash prefix
    matched nothing and the scope silently admitted everything. That separator
    bug has now shipped twice in this codebase (A-404 and the 000-PRIORITY boost
    before it) — both times as a behaviour difference with no error message."""
    env = D.resolve(_Cfg(allowed_path_prefixes=["P3-BRAIN/"]), env={}, warn=False)
    kept, _ = _apply([_hit("P3-BRAIN\\x\\LAIR.md"), _hit("P1-OTHER/y.md")], env)
    assert [h.rel_path for h in kept] == ["P3-BRAIN\\x\\LAIR.md"]


def test_permissions_apply_even_in_full_mode():
    """Permissions are STANDING; blinding is per-request. If scope evaporated the
    moment a request stopped asking to be blinded, it would not be scope."""
    env = D.resolve(_Cfg(allowed_path_prefixes=["ok/"]), env={"LBRAIN_DISCLOSURE": "full"},
                    warn=False)
    kept, w = _apply([_hit("ok/a.md"), _hit("no/b.md")], env)
    assert [h.rel_path for h in kept] == ["ok/a.md"] and w.by_permission == 1


def test_force_priority_only_cannot_be_turned_off_by_a_call():
    env = D.resolve(_Cfg(force_priority_only=True), env={}, warn=False)
    kept, _ = _apply([_hit("a.md", is_priority=True), _hit("b.md", is_priority=False)], env)
    assert [h.rel_path for h in kept] == ["a.md"]


# ==========================================================================
# enforcement reaches BOTH retrieval paths
# ==========================================================================


def _store_with(rel_paths_and_class) -> Store:
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    for rel, cls in rel_paths_and_class:
        st.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime, disclosure) "
            "VALUES (?, ?, ?, 'h', 0, ?)", (rel, f"/tmp/{rel}", rel, cls))
        cur = st.db.execute(
            "INSERT INTO chunks (rel_path, chunk_idx, text, token_count, chunk_hash) "
            "VALUES (?, 0, ?, 3, 'h')", (rel, "subsidised storage margin analysis"))
        st.db.execute(
            "INSERT INTO fts_chunks (rowid, text, rel_path, chunk_idx) VALUES (?, ?, ?, 0)",
            (cur.lastrowid, "subsidised storage margin analysis", rel))
    st.db.commit()
    return st


def test_the_keyword_path_enforces_the_same_envelope():
    """If it did not, the control would have a documented bypass reachable by one
    tool call — the A-410 shape, leaking corpus instead of a badge."""
    st = _store_with([("paper.md", "artifact"), ("plan.md", "proposal")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)

    blinded = keyword_only(st, "subsidised storage", k=10, envelope=env)
    assert [h.rel_path for h in blinded] == ["paper.md"]
    assert blinded.withheld.total == 1

    # NEGATIVE CONTROL — no envelope, both records reachable on this same path.
    assert len(keyword_only(st, "subsidised storage", k=10)) == 2
    st.close()


def test_withheld_travels_back_with_the_hits():
    st = _store_with([("paper.md", "artifact"), ("plan.md", "proposal")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = keyword_only(st, "subsidised storage", k=10, envelope=env)
    assert isinstance(hits, list), "HitList must remain a plain list to every caller"
    assert hits.withheld.total == 1
    assert hits.envelope.mode == "independent"
    st.close()


def test_no_envelope_leaves_the_retrieval_path_untouched():
    st = _store_with([("a.md", ""), ("b.md", "")])
    hits = keyword_only(st, "subsidised storage", k=10)
    assert len(hits) == 2
    assert hits.withheld is None, "no envelope must mean no filter ran at all"
    st.close()


# ==========================================================================
# the blinded reader must KNOW it is blinded
# ==========================================================================


def test_the_served_response_says_what_was_withheld():
    """An agent handed a silently-thinned corpus does not conclude 'I am missing
    context' — it answers confidently from the remainder."""
    from lbrain.serve import blinding_notice

    st = _store_with([("paper.md", "artifact"), ("plan.md", "proposal")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = keyword_only(st, "subsidised storage", k=10, envelope=env)
    note = blinding_notice(hits)
    assert "WITHHELD" in note and "independent" in note and "1 proposal" in note
    st.close()


def test_the_notice_fires_even_when_everything_was_withheld():
    """'Nothing came back' and 'everything was withheld' are the two readings a
    blinded agent must never confuse."""
    from lbrain.serve import blinding_notice

    st = _store_with([("plan.md", "proposal")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = keyword_only(st, "subsidised storage", k=10, envelope=env)
    assert list(hits) == []
    assert "WITHHELD" in blinding_notice(hits)
    st.close()


def test_the_RENDERED_response_carries_the_notice_when_nothing_survived():
    """The second hole: the previous test called blinding_notice() directly, so a
    mutation that suppressed the notice inside render_response — precisely when
    zero records survived, the case that matters most — sailed through it. Assert
    through the real rendering path."""
    from lbrain.serve import render_response

    class _Serve:
        amp_budget_chars = 6000
        serve_chunk_chars = 700
        serve_admissibility = False
        serve_staleness = False
        amp_provenance = False
        core_memory_path = ""
        gate_min_near = 3
        gate_density = 0.5

    st = _store_with([("plan.md", "proposal")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = keyword_only(st, "subsidised storage", k=10, envelope=env)
    assert list(hits) == []  # precondition: everything was withheld

    out = render_response(_Serve(), hits, "subsidised storage", include_core=False)
    assert "WITHHELD" in out, "an empty blinded response must still say it was blinded"

    # NEGATIVE CONTROL — an unblinded render of the same corpus says nothing.
    plain = keyword_only(st, "subsidised storage", k=10)
    assert "WITHHELD" not in render_response(_Serve(), plain, "subsidised storage",
                                             include_core=False)
    st.close()


def test_an_all_unclassified_withholding_says_how_to_classify():
    from lbrain.serve import blinding_notice

    st = _store_with([("a.md", ""), ("b.md", "")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    hits = keyword_only(st, "subsidised storage", k=10, envelope=env)
    note = blinding_notice(hits)
    assert "disclosure_default" in note and "unclassified" in note
    st.close()


def test_no_notice_when_nothing_was_withheld():
    from lbrain.serve import blinding_notice

    st = _store_with([("paper.md", "artifact")])
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    assert blinding_notice(keyword_only(st, "subsidised storage", k=10, envelope=env)) == ""
    st.close()


# ==========================================================================
# core memory — the one injection path retrieval filtering never sees
# ==========================================================================


CORE = """# Core memory

## Doctrine — always delivered
- Proof-first; never fabricate; report faithfully.

## Context — project state
- Matrix B: cross-architecture blind spots are REAL here.
"""


def test_the_split_puts_standing_orders_in_doctrine_and_conclusions_in_context():
    doctrine, context = D.split_core(CORE)
    assert "never fabricate" in doctrine
    assert "Matrix B" not in doctrine, "a revisable conclusion is not doctrine"
    assert "Matrix B" in context
    assert "never fabricate" not in context


def test_an_unmarked_core_memory_is_ENTIRELY_context():
    """Fail closed. The recoverable error is a persona noticing its doctrine is
    missing; the unrecoverable one is a reviewer silently handed the conclusion it
    was convened to check. Measured on the live file 2026-07-31: ~8 of 11 lines
    were conclusions and framing, and NONE of it was marked."""
    doctrine, context = D.split_core("- Leading with LBrain.\n- Moat = THREE findings.")
    assert doctrine == ""
    assert "Moat" in context


def test_content_before_the_first_heading_is_context():
    doctrine, context = D.split_core("preamble line\n\n## Doctrine\n- rule")
    assert "preamble" in context and "preamble" not in doctrine
    assert "rule" in doctrine


def test_a_non_doctrine_heading_CLOSES_a_doctrine_section():
    doctrine, context = D.split_core(
        "## Doctrine\n- rule\n## Findings\n- conclusion\n")
    assert "rule" in doctrine and "conclusion" not in doctrine
    assert "conclusion" in context


def test_heading_matching_is_by_CONTAINMENT_so_agentx_needs_no_change():
    """`personas/_shared/DOCTRINE.md` opens `## Binding doctrine — every persona,
    always on`. Matching on containment rather than prefix means that file
    classifies correctly today with zero edits, and the router lane's
    concatenation of four sources composes sections instead of fighting them."""
    doctrine, context = D.split_core(
        "## Binding doctrine — every persona, always on\n- Verify LIVE state.\n")
    assert "Verify LIVE state" in doctrine and context == ""


def test_doctrine_is_delivered_in_EVERY_mode():
    """Doctrine reaching the agent in every mode IS the exoskeleton — a blinded
    reviewer still has to know its standards."""
    for mode in D.MODES:
        env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": mode}, warn=False)
        assert D.core_admits_context(env) == (mode in (D.MODE_FULL, D.MODE_COLLABORATIVE)), mode


def test_independent_delivers_doctrine_and_withholds_context(tmp_path):
    from lbrain import amp

    p = tmp_path / "CORE.md"
    p.write_text(CORE, encoding="utf-8")
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    w = D.Withheld()

    blinded = amp.core_block(str(p), 4000, envelope=env, withheld=w)
    assert "never fabricate" in blinded, "the persona must keep its standing orders"
    assert "Matrix B" not in blinded, "a blinded reviewer must not receive the conclusion"
    assert "DOCTRINE ONLY" in blinded, "and must be told the block was cut"
    assert w.core_context_chars > 0

    # NEGATIVE CONTROL — unblinded delivers both, so "absent" is not just an
    # empty file or a broken reader.
    full = amp.core_block(str(p), 4000,
                          envelope=D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "full"}, warn=False),
                          withheld=D.Withheld())
    assert "never fabricate" in full and "Matrix B" in full


def test_no_envelope_leaves_core_memory_BYTE_IDENTICAL(tmp_path):
    """Every call that predates this layer must be unchanged — and "unchanged"
    means the bytes, not merely the set of facts present.

    The fixture puts CONTEXT FIRST on purpose. An earlier version of this test
    used a doctrine-first file, so splitting and rejoining happened to reproduce
    the original order and a mutation that split unconditionally SURVIVED. Core
    memory is injected ahead of every query; silently reordering it is a real
    behaviour change that no assertion about content would ever catch.
    """
    from lbrain import amp

    interleaved = (
        "# Core memory\n\n"
        "## Context — project state\n- Matrix B: blind spots are REAL here.\n\n"
        "## Doctrine — always delivered\n- Proof-first; never fabricate.\n"
    )
    p = tmp_path / "CORE.md"
    p.write_text(interleaved, encoding="utf-8")

    out = amp.core_block(str(p), 4000)
    assert out == "🧠 Core memory (always-on):\n" + interleaved.strip() + "\n", \
        "an envelope-free call must return the file verbatim, in file order"
    assert "DOCTRINE ONLY" not in out

    # NEGATIVE CONTROL — with an envelope the reordering IS expected, so the
    # equality above is pinning the no-envelope path specifically.
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "full"}, warn=False)
    assert amp.core_block(str(p), 4000, envelope=env, withheld=D.Withheld()) != out


def test_core_withholding_is_announced_even_when_no_record_was_withheld():
    """The core block bypasses retrieval, so silence here is the worst silence:
    the agent would receive a doctrine-only view with nothing telling it so."""
    w = D.Withheld()
    w.core_context_chars = 412
    note = w.notice("independent")
    assert "Core memory" in note and "412" in note and "doctrine delivered" in note


def test_truncation_can_no_longer_evict_doctrine(tmp_path):
    """A-421 was exactly this: the char budget silently ate the newest lines
    because corrections are appended last. Splitting BEFORE truncating means a
    long context block can no longer push standing orders out of the budget."""
    from lbrain import amp

    p = tmp_path / "CORE.md"
    p.write_text("## Context\n" + ("- filler line that is quite long\n" * 200)
                 + "\n## Doctrine\n- never fabricate\n", encoding="utf-8")
    env = D.resolve(_Cfg(), env={"LBRAIN_DISCLOSURE": "independent"}, warn=False)
    out = amp.core_block(str(p), 200, envelope=env, withheld=D.Withheld())
    assert "never fabricate" in out, "doctrine must survive a budget the context blew"


# ==========================================================================
# classification comes from the file
# ==========================================================================


def test_frontmatter_disclosure_is_parsed_and_stored():
    d = Path(tempfile.mkdtemp())
    p = d / "x.md"
    p.write_text("---\nname: x\ndisclosure: Artifact\n---\nbody", encoding="utf-8")
    doc = parse(p, repo_root=d)
    assert doc.disclosure == "artifact", "case-insensitive, normalised"

    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    st.upsert_doc(doc)
    st.db.commit()
    assert st.disclosure_classes() == {doc.rel_path: "artifact"}
    st.close()


def test_an_unknown_disclosure_value_is_not_trusted():
    """An author's typo must not mint a class the filter has never heard of and
    therefore cannot reason about. Falls to unclassified = withheld."""
    d = Path(tempfile.mkdtemp())
    p = d / "x.md"
    p.write_text("---\nname: x\ndisclosure: public\n---\nbody", encoding="utf-8")
    assert parse(p, repo_root=d).disclosure == ""


def test_changing_only_the_disclosure_line_is_detected_on_reimport():
    """The A-401 trap again: classification lives entirely in frontmatter, so a
    reclassification changes no chunk and lands in import's 'unchanged' branch."""
    d = Path(tempfile.mkdtemp())
    p = d / "x.md"
    p.write_text("---\nname: x\ndisclosure: proposal\n---\nbody", encoding="utf-8")
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    doc = parse(p, repo_root=d)
    st.upsert_doc(doc)
    st.db.commit()

    p.write_text("---\nname: x\ndisclosure: artifact\n---\nbody", encoding="utf-8")
    doc2 = parse(p, repo_root=d)
    assert doc2.doc_hash == doc.doc_hash, "precondition: the body did not change"
    assert st.doc_metadata_differs(doc2), "a reclassification must be seen as a change"
    st.upsert_doc(doc2)
    st.db.commit()
    assert st.disclosure_classes() == {doc2.rel_path: "artifact"}
    st.close()


def test_a_preexisting_brain_backfills_the_disclosure_column():
    """The third hole, and the one that would have bitten the LIVE brain.

    A doc that already declared `disclosure:` BEFORE the column existed has the
    class in its metadata JSON and '' in the freshly-ALTERed column. Body
    unchanged, metadata unchanged — so without an explicit column comparison,
    import's 'unchanged' branch skips it and the document stays UNCLASSIFIED
    forever while its own frontmatter says otherwise. Silent, permanent, and on
    a corpus of ~2,000 rows it would look like the feature simply did not work.
    """
    import json

    d = Path(tempfile.mkdtemp())
    p = d / "x.md"
    p.write_text("---\nname: x\ndisclosure: artifact\n---\nbody", encoding="utf-8")
    doc = parse(p, repo_root=d)

    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    # Simulate the pre-migration row: correct metadata, empty column.
    st.db.execute(
        "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime, is_priority, "
        "doc_type, metadata, disclosure) VALUES (?, ?, ?, ?, ?, 0, '', ?, '')",
        (doc.rel_path, str(doc.path), doc.title, doc.doc_hash, doc.mtime,
         json.dumps({"name": "x", "disclosure": "artifact"})),
    )
    st.db.commit()
    assert st.disclosure_classes() == {}, "precondition: the column is empty"

    assert st.doc_metadata_differs(doc), "the empty column must be seen as drift"
    st.upsert_doc(doc)
    st.db.commit()
    assert st.disclosure_classes() == {doc.rel_path: "artifact"}
    st.close()


# ==========================================================================
# a mistyped persona fails closed — but no longer silently
# ==========================================================================


def test_an_unknown_persona_warns_rather_than_failing_silently():
    """Recommended by the router lane. Wrong values already failed CLOSED (they
    proved it across 12 hostile strings); the gap was that the author of a
    mistyped name concludes their beliefs were LOST."""
    from lbrain.search import check_persona

    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    st.db.execute("INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) "
                  "VALUES ('b.md','/tmp/b.md','b','h',0)")
    st.replace_belief({"belief_id": "b", "rel_path": "b.md", "persona": "cto",
                       "state": "draft", "subject": "s", "claim": "c", "created": "2026-07-01"}, [])
    st.db.commit()

    check_persona._warned = set()
    assert "case-sensitive" in check_persona(st, "CTO"), "wrong case must be surfaced"
    check_persona._warned = set()
    assert check_persona(st, "cto") == "", "the real author must not be warned at"
    st.close()


HOSTILE_PERSONAS = ["", " ", "CTO", "cto ", "ct", "ctoo", "*", "../cto",
                    "cto' OR '1'='1", "cto\x00", "%", "_"]


@pytest.mark.parametrize("bad", HOSTILE_PERSONAS)
def test_a_hostile_persona_string_never_reveals_a_draft(bad):
    """Ported from the router lane's MCP suite as unit tests — same twelve values,
    no process spawn. The guarantee rested on an `!=` that no test defended."""
    from lbrain.search import apply_belief_visibility

    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    for rel in ("cto/draft.md", "shared/fact.md"):
        st.db.execute("INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) "
                      "VALUES (?,?,?,'h',0)", (rel, f"/tmp/{rel}", rel))
    st.replace_belief({"belief_id": "d", "rel_path": "cto/draft.md", "persona": "cto",
                       "state": "draft", "subject": "s", "claim": "c", "created": "2026-07-01"}, [])
    st.db.commit()

    hits = [Hit(rel_path="cto/draft.md", chunk_idx=0, text="x", title="draft", score=1.0),
            Hit(rel_path="shared/fact.md", chunk_idx=0, text="y", title="fact", score=0.5)]
    kept = apply_belief_visibility(st, hits, bad, rank=True)
    titles = [h.title for h in kept]
    assert "draft" not in titles, f"{bad!r} revealed another persona's draft"
    # NEGATIVE CONTROL — shared truth stayed visible, so "sees nothing" is not
    # merely a query that matched nothing.
    assert titles == ["fact"], "negative control failed — the test proves nothing"
    st.close()


def test_core_memory_truncation_is_announced(tmp_path, capsys):
    """A-421 was a SILENT truncation: the char budget ate the newest, most-hedged
    lines because corrections are appended last, and the block still looked
    complete. This session reproduced the setup by accident — two classification
    headings pushed the live file from 1,519 to 1,699 chars against a 1,600
    budget. Silent dropping from EVERY query is not an acceptable failure mode.
    """
    from lbrain import amp

    p = tmp_path / "CORE.md"
    p.write_text("- line one is quite long indeed\n" * 40, encoding="utf-8")
    amp._CORE_TRUNCATION_WARNED.clear()

    out = amp.core_block(str(p), 200)
    err = capsys.readouterr().err
    assert out.rstrip().endswith("…")             # it did truncate
    assert "DROPPED from every query" in err       # and it said so
    assert "core_memory_chars" in err              # and named the fix

    # Warn ONCE per (path, budget) — a long-lived MCP server must not spam.
    amp.core_block(str(p), 200)
    assert "DROPPED" not in capsys.readouterr().err

    # NEGATIVE CONTROL — a file inside its budget truncates nothing and is silent.
    amp._CORE_TRUNCATION_WARNED.clear()
    small = tmp_path / "S.md"
    small.write_text("- short\n", encoding="utf-8")
    assert not amp.core_block(str(small), 200).rstrip().endswith("…")
    assert "DROPPED" not in capsys.readouterr().err
