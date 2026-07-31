"""Per-agent belief accumulation — draft isolation, the promotion gate, and the
structural defence against self-citation loops.

Every test here was MUTATION-TESTED: the behaviour it covers was deliberately
broken and the test confirmed to fail before being kept. A test that cannot fail
is not evidence, and this repo has already shipped one test that passed while its
bug was live.
"""

import datetime
import tempfile
from pathlib import Path

import pytest

from lbrain import beliefs as B
from lbrain.index import parse
from lbrain.search import Hit, apply_belief_visibility, keyword_only
from lbrain.store import Store

TODAY = datetime.date(2026, 7, 31)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _b(bid, persona="cto", state=B.STATE_PROMOTED, evidence=(), **kw):
    """A belief with everything G6 needs, so a test can isolate ONE check."""
    return B.Belief(
        belief_id=bid,
        rel_path=f"{bid}.md",
        persona=persona,
        state=state,
        subject=kw.pop("subject", bid),
        claim=kw.pop("claim", f"claim of {bid}"),
        created=kw.pop("created", "2026-07-01"),
        evidence=[
            e if isinstance(e, B.EvidenceRef) else B.EvidenceRef(ref=e, kind="link", verified=True)
            for e in evidence
        ],
        **kw,
    )


def _view(beliefs=(), docs=(), authors=None):
    return B.DictCorpusView(
        docs=set(docs),
        beliefs={b.belief_id: b for b in beliefs},
        authors=dict(authors or {}),
    )


def _codes(res):
    return {c.code: c.passed for c in res.checks}


def _store_with_docs(rel_paths) -> Store:
    """A Store whose docs table has these rows (beliefs FK requires them)."""
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    for rel in rel_paths:
        st.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) "
            "VALUES (?, ?, ?, ?, 0)",
            (rel, f"/tmp/{rel}", rel, "h"),
        )
    st.db.commit()
    return st


def _put(store, bid, rel, persona, state, evidence=()):
    store.replace_belief(
        {"belief_id": bid, "rel_path": rel, "persona": persona, "state": state,
         "subject": bid, "claim": "c", "created": "2026-07-01"},
        [(r, "link", True) for r in evidence],
    )
    store.db.commit()


# ==========================================================================
# parsing — a belief is a markdown file; the DB is a projection
# ==========================================================================


def test_a_plain_doc_is_not_a_belief():
    d = Path(tempfile.mkdtemp())
    p = d / "note.md"
    p.write_text("---\nname: note\ntype: project\n---\nbody", encoding="utf-8")
    assert B.from_doc(parse(p, repo_root=d)) is None


def test_belief_frontmatter_becomes_a_belief():
    d = Path(tempfile.mkdtemp())
    p = d / "margin.md"
    p.write_text(
        "---\nname: margin\ntype: belief\npersona: cfo\nstate: draft\n"
        "subject: storage\nclaim: it inverts\nconfidence: high\ncreated: 2026-07-31\n"
        "evidence:\n  - '[[turbo-credit]]'\n---\nbody",
        encoding="utf-8",
    )
    b = B.from_doc(parse(p, repo_root=d))
    assert (b.belief_id, b.persona, b.state, b.subject) == ("margin", "cfo", "draft", "storage")
    assert b.evidence[0].slug == "turbo-credit"
    assert b.required_roots == 2  # confidence: high raises the bar


def test_unknown_lifecycle_state_falls_back_to_private():
    """An unrecognised `state:` must not become shared truth by accident.

    Fail-closed: a typo like `state: promted` is the operator believing a belief
    is public. Defaulting to `draft` keeps it private, which is the recoverable
    direction of the error.
    """
    d = Path(tempfile.mkdtemp())
    p = d / "x.md"
    p.write_text("---\nname: x\ntype: belief\npersona: cto\nstate: promted\n---\nb", encoding="utf-8")
    assert B.from_doc(parse(p, repo_root=d)).state == B.STATE_DRAFT


def test_external_evidence_is_unattested_unless_the_author_says_otherwise():
    """Doctrine rule 9 as a data requirement: a URL that merely looks plausible
    is not a dereference. `alerts@metavolve-labs.com` passed every layer that
    treated an identifier as an opaque string."""
    refs = B.parse_evidence([
        "https://turbo.ardrive.io/price/bytes/1048576",
        {"ref": "https://arweave.net/tx/abc", "verified": "2026-07-31"},
        "[[some-lair]]",
    ])
    assert [(r.kind, r.verified) for r in refs] == [
        ("external", False), ("external", True), ("link", True)
    ]


# ==========================================================================
# THE core mechanism: corroboration is counted by roots, not by restatement
# ==========================================================================


def test_restating_one_source_five_times_is_still_one_root():
    """The self-citation loop, in its purest form.

    Five beliefs all trace to a single observation. A system that counted
    SUPPORTING RECORDS would read this as overwhelming corroboration. Counting
    distinct roots reports the truth: one source, restated.
    """
    chain = [_b("r1", evidence=["obs"])]
    for i in range(2, 6):
        chain.append(_b(f"r{i}", evidence=[f"r{i - 1}"]))
    view = _view(chain, docs=["obs"], authors={"obs": "tad"})

    g = B.ground(chain[-1], view, max_depth=99)
    assert g.roots == {"obs"}, "restatement must not multiply corroboration"
    assert g.depth == 5


def test_a_belief_citing_itself_is_a_cycle_not_a_ground():
    b = _b("ouroboros", evidence=["ouroboros"])
    view = _view([b])
    g = B.ground(b, view)
    assert g.cycle == ["ouroboros"]
    assert g.roots == set()
    assert _codes(B.gate(b, view, today=TODAY))["G2"] is False


def test_a_two_belief_cycle_is_detected():
    a = _b("alpha", evidence=["beta"])
    beta = _b("beta", evidence=["alpha"])
    view = _view([a, beta])
    g = B.ground(a, view)
    assert g.cycle == ["alpha"]
    assert g.roots == set()


def test_a_draft_can_never_be_evidence_even_for_its_own_author():
    """Bootstrapping unreviewed speculation into support for a promotion is the
    loop. Refused regardless of authorship — an agent cannot vouch for itself."""
    draft = _b("hunch", persona="cto", state=B.STATE_DRAFT, evidence=["obs"])
    claim = _b("conclusion", persona="cto", evidence=["hunch"])
    view = _view([draft, claim], docs=["obs"])
    g = B.ground(claim, view)
    assert g.unsound == ["hunch (draft)"]
    assert g.roots == set()
    assert _codes(B.gate(claim, view, today=TODAY))["G2"] is False


def test_a_retracted_belief_can_never_be_evidence():
    dead = _b("wrong", state=B.STATE_RETRACTED, evidence=["obs"])
    claim = _b("built-on-sand", evidence=["wrong"])
    view = _view([dead, claim], docs=["obs"])
    assert B.ground(claim, view).unsound == ["wrong (retracted)"]


# ==========================================================================
# the promotion gate — G1..G7
# ==========================================================================


def test_gate_passes_a_well_grounded_belief():
    b = _b("grounded", persona="cfo", state=B.STATE_DRAFT, evidence=["obs"])
    view = _view([b], docs=["obs"], authors={"obs": "tad"})
    res = B.gate(b, view, today=TODAY)
    assert res.passed, res.report()
    assert res.roots == {"obs"} and res.depth == 1


def test_G1_a_citation_that_resolves_to_nothing_blocks_promotion():
    b = _b("ghost", state=B.STATE_DRAFT, evidence=["a-lair-that-does-not-exist"])
    res = B.gate(b, _view([b]), today=TODAY)
    assert _codes(res)["G1"] is False
    assert "unresolved" in res.report()


def test_G1_an_unattested_external_url_blocks_promotion():
    ref = B.EvidenceRef(ref="https://example.invalid/x", kind="external", verified=False)
    b = _b("web", state=B.STATE_DRAFT, evidence=[ref])
    assert _codes(B.gate(b, _view([b]), today=TODAY))["G1"] is False

    ok = B.EvidenceRef(ref="https://example.invalid/x", kind="external", verified=True)
    b2 = _b("web2", state=B.STATE_DRAFT, evidence=[ok])
    assert _codes(B.gate(b2, _view([b2]), today=TODAY))["G1"] is True


def test_G3_a_belief_too_far_from_any_observation_is_speculation():
    chain = [_b("g1", evidence=["obs"])]
    for i in range(2, 7):
        chain.append(_b(f"g{i}", evidence=[f"g{i - 1}"]))
    view = _view(chain, docs=["obs"], authors={"obs": "tad"})
    res = B.gate(chain[-1], view, today=TODAY)
    assert _codes(res)["G3"] is False
    assert res.depth == 6  # measured even when it fails — the number is the finding


def test_G3_a_belief_with_no_observation_beneath_it_fails():
    b = _b("floating", state=B.STATE_DRAFT, evidence=[])
    res = B.gate(b, _view([b]), today=TODAY)
    assert _codes(res)["G3"] is False
    assert res.depth is None


def test_G4_high_confidence_demands_two_distinct_roots():
    one = _b("bold", state=B.STATE_DRAFT, confidence="high", evidence=["obs"])
    view = _view([one], docs=["obs"], authors={"obs": "tad"})
    assert _codes(B.gate(one, view, today=TODAY))["G4"] is False

    two = _b("bold2", state=B.STATE_DRAFT, confidence="high", evidence=["obs", "obs2"])
    view2 = _view([two], docs=["obs", "obs2"], authors={"obs": "tad", "obs2": "tad"})
    assert _codes(B.gate(two, view2, today=TODAY))["G4"] is True


def test_G4_medium_confidence_is_satisfied_by_one_verified_root():
    """A single measurement is legitimate evidence. Demanding two would block
    'AR was ~$2 on 2026-07-07' — a live-checked fact with exactly one source."""
    b = _b("measured", state=B.STATE_DRAFT, confidence="medium", evidence=["obs"])
    view = _view([b], docs=["obs"], authors={"obs": "tad"})
    assert _codes(B.gate(b, view, today=TODAY))["G4"] is True


def test_G4_an_agent_cannot_corroborate_itself_with_its_own_notes():
    """Every root written by the promoting persona = no independent support."""
    b = _b("selfish", persona="cto", state=B.STATE_DRAFT, evidence=["my-own-note"])
    view = _view([b], docs=["my-own-note"], authors={"my-own-note": "cto"})
    res = B.gate(b, view, today=TODAY)
    assert _codes(res)["G4"] is False
    assert "authored by 'cto'" in res.report()


def test_G5_an_undeclared_contradiction_is_refused():
    incumbent = _b("old-view", subject="storage")
    rival = _b("new-view", state=B.STATE_DRAFT, subject="storage", evidence=["obs"])
    view = _view([incumbent, rival], docs=["obs"], authors={"obs": "tad"})
    res = B.gate(rival, view, today=TODAY)
    assert _codes(res)["G5"] is False
    assert "old-view" in res.report()


def test_G5_declaring_the_supersession_unblocks_it():
    incumbent = _b("old-view", subject="storage")
    rival = _b("new-view", state=B.STATE_DRAFT, subject="storage", evidence=["obs"])
    rival.supersedes = ["[[old-view]]"]  # written the way an author writes it
    view = _view([incumbent, rival], docs=["obs"], authors={"obs": "tad"})
    res = B.gate(rival, view, today=TODAY)
    assert res.passed, res.report()


def test_G6_missing_provenance_fails_loud_and_names_the_field():
    b = B.Belief(belief_id="anon", rel_path="anon.md", state=B.STATE_DRAFT,
                 evidence=[B.EvidenceRef("obs", "link", True)])
    res = B.gate(b, _view([b], docs=["obs"]), today=TODAY)
    assert _codes(res)["G6"] is False
    for field in ("persona", "created", "subject", "claim"):
        assert field in res.report()


def test_G7_a_belief_that_expired_before_promotion_is_refused():
    b = _b("stale", state=B.STATE_DRAFT, evidence=["obs"], verify_by="2026-07-01")
    view = _view([b], docs=["obs"], authors={"obs": "tad"})
    assert _codes(B.gate(b, view, today=TODAY))["G7"] is False
    assert _codes(B.gate(b, view, today=datetime.date(2026, 6, 1)))["G7"] is True


def test_impact_action_needs_a_countersignature_from_another_lane():
    """MemTX's action-safety class. We refuse rather than invent an approver —
    a plausible default that wires a side effect is a bug, not a convenience."""
    base = dict(state=B.STATE_DRAFT, impact="action", persona="cto",
                evidence=["obs", "obs2"])
    view = _view(docs=["obs", "obs2"], authors={"obs": "tad", "obs2": "tad"})

    none = _b("act", **base)
    view.beliefs[none.belief_id] = none
    assert _codes(B.gate(none, view, today=TODAY))["G7a"] is False

    self_signed = _b("act2", countersigned_by="cto", **base)
    view.beliefs[self_signed.belief_id] = self_signed
    assert _codes(B.gate(self_signed, view, today=TODAY))["G7a"] is False

    signed = _b("act3", countersigned_by="cfo", **base)
    view.beliefs[signed.belief_id] = signed
    assert B.gate(signed, view, today=TODAY).passed


# ==========================================================================
# retraction — cascade repair, deliberately non-destructive
# ==========================================================================


def test_retraction_flags_transitive_dependants_and_skips_drafts():
    root = _b("root", evidence=["obs"])
    mid = _b("mid", evidence=["root"])
    leaf = _b("leaf", evidence=["mid"])
    sketch = _b("sketch", state=B.STATE_DRAFT, evidence=["root"])
    unrelated = _b("unrelated", evidence=["obs"])
    all_b = [root, mid, leaf, sketch, unrelated]

    hit = B.cascade_targets("root", all_b)
    assert hit == ["leaf", "mid"], "the closure must be transitive"
    assert "sketch" not in hit    # already private; the gate re-checks it on promotion
    assert "unrelated" not in hit


# ==========================================================================
# draft isolation — the disclosure boundary, on BOTH retrieval paths
# ==========================================================================


def test_one_agents_draft_is_invisible_to_another():
    st = _store_with_docs(["cfo/hunch.md", "shared/fact.md"])
    _put(st, "hunch", "cfo/hunch.md", "cfo", B.STATE_DRAFT)
    hits = [
        Hit(rel_path="cfo/hunch.md", chunk_idx=0, text="x", title="hunch", score=1.0),
        Hit(rel_path="shared/fact.md", chunk_idx=0, text="y", title="fact", score=0.5),
    ]
    seen = apply_belief_visibility(st, list(hits), "cto", rank=True)
    assert [h.title for h in seen] == ["fact"]
    st.close()


def test_an_agent_sees_its_own_draft_and_it_is_labelled_as_such():
    st = _store_with_docs(["cfo/hunch.md"])
    _put(st, "hunch", "cfo/hunch.md", "cfo", B.STATE_DRAFT)
    h = Hit(rel_path="cfo/hunch.md", chunk_idx=0, text="x", title="hunch", score=1.0)
    seen = apply_belief_visibility(st, [h], "cfo", rank=True)
    assert len(seen) == 1
    assert "draft" in seen[0].boosts, "an agent must be TOLD it is reading itself"
    st.close()


def test_with_no_persona_no_draft_is_visible_to_anyone():
    """The default for every call that predates this layer. Isolation must not
    depend on the caller remembering to pass something."""
    st = _store_with_docs(["cfo/hunch.md"])
    _put(st, "hunch", "cfo/hunch.md", "cfo", B.STATE_DRAFT)
    h = Hit(rel_path="cfo/hunch.md", chunk_idx=0, text="x", title="hunch", score=1.0)
    assert apply_belief_visibility(st, [h], None, rank=True) == []
    st.close()


def test_draft_isolation_also_holds_on_the_keyword_path():
    """Splitting disclosure across the two retrieval paths is exactly how the
    SUPERSEDED badge went missing from one of them (A-410). A leak here is worse
    than a missing badge."""
    st = _store_with_docs(["cfo/hunch.md"])
    st.db.execute(
        "INSERT INTO chunks (rel_path, chunk_idx, text, token_count, chunk_hash) "
        "VALUES (?, 0, ?, 3, 'h')", ("cfo/hunch.md", "subsidised storage inverts margin"))
    cid = st.db.execute("SELECT chunk_id FROM chunks").fetchone()["chunk_id"]
    st.db.execute(
        "INSERT INTO fts_chunks (rowid, text, rel_path, chunk_idx) VALUES (?, ?, ?, 0)",
        (cid, "subsidised storage inverts margin", "cfo/hunch.md"))
    _put(st, "hunch", "cfo/hunch.md", "cfo", B.STATE_DRAFT)

    assert keyword_only(st, "subsidised storage", k=5, persona="cfo"), "author must see it"
    assert keyword_only(st, "subsidised storage", k=5, persona="cto") == []
    assert keyword_only(st, "subsidised storage", k=5) == []
    st.close()


def test_a_promoted_belief_is_visible_to_every_persona_and_marked():
    st = _store_with_docs(["cfo/shipped.md"])
    _put(st, "shipped", "cfo/shipped.md", "cfo", B.STATE_PROMOTED)
    h = Hit(rel_path="cfo/shipped.md", chunk_idx=0, text="x", title="shipped", score=1.0)
    seen = apply_belief_visibility(st, [h], "cto", rank=True)
    assert len(seen) == 1 and "belief" in seen[0].boosts
    st.close()


def test_a_retracted_belief_is_buried_but_still_retrievable():
    """Deleting a corrected belief deletes the negative example, and the agent
    regenerates the same error. Bury, never delete — the same argument the
    corpus already makes for superseded text."""
    st = _store_with_docs(["cto/wrong.md"])
    _put(st, "wrong", "cto/wrong.md", "cto", B.STATE_RETRACTED)
    h = Hit(rel_path="cto/wrong.md", chunk_idx=0, text="x", title="wrong", score=1.0)
    seen = apply_belief_visibility(st, [h], "cto", rank=True, penalty=0.25)
    assert len(seen) == 1, "a retracted belief must remain retrievable"
    assert seen[0].score == pytest.approx(0.25)
    assert seen[0].boosts["retracted"] == 0.25
    st.close()


def test_needs_review_is_flagged_without_an_unmeasured_score_change():
    """It was not withdrawn — something beneath it was. Say so; do not invent a
    ranking multiplier nobody measured (doctrine rule 4)."""
    st = _store_with_docs(["cto/shaky.md"])
    _put(st, "shaky", "cto/shaky.md", "cto", B.STATE_NEEDS_REVIEW)
    h = Hit(rel_path="cto/shaky.md", chunk_idx=0, text="x", title="shaky", score=1.0)
    seen = apply_belief_visibility(st, [h], "cto", rank=True)
    assert seen[0].score == pytest.approx(1.0)
    assert "needs_review" in seen[0].boosts
    st.close()


def test_a_corpus_with_no_beliefs_is_completely_unaffected():
    """The no-regression guarantee: on today's live brain this layer is inert."""
    st = _store_with_docs(["a.md", "b.md"])
    hits = [
        Hit(rel_path="a.md", chunk_idx=0, text="x", title="a", score=1.0),
        Hit(rel_path="b.md", chunk_idx=0, text="y", title="b", score=0.5),
    ]
    out = apply_belief_visibility(st, hits, None, rank=True)
    assert out == hits and all(not h.boosts for h in out)
    st.close()


# ==========================================================================
# storage projection
# ==========================================================================


def test_projection_round_trips_through_the_store():
    st = _store_with_docs(["x.md"])
    _put(st, "x", "x.md", "cto", B.STATE_DRAFT, evidence=["obs", "other"])
    row = st.belief_row("x")
    b = B.from_row(row, st.belief_evidence_rows("x"))
    assert (b.persona, b.state) == ("cto", B.STATE_DRAFT)
    assert sorted(e.slug for e in b.evidence) == ["obs", "other"]
    st.close()


def test_renaming_a_beliefs_slug_does_not_orphan_the_old_row():
    """`name:` is editable. Without clearing the previous row the UNIQUE on
    rel_path fails and the belief silently stops updating."""
    st = _store_with_docs(["x.md"])
    _put(st, "old-name", "x.md", "cto", B.STATE_DRAFT)
    _put(st, "new-name", "x.md", "cto", B.STATE_PROMOTED)
    assert [r["belief_id"] for r in st.belief_rows()] == ["new-name"]
    assert st.belief_states() == {"x.md": ("cto", B.STATE_PROMOTED)}
    st.close()


def test_removing_type_belief_removes_the_projection():
    st = _store_with_docs(["x.md"])
    _put(st, "x", "x.md", "cto", B.STATE_DRAFT)
    st.delete_belief_for_path("x.md")
    st.db.commit()
    assert st.belief_states() == {}
    st.close()


def test_deleting_the_source_file_cascades_the_belief_away():
    """Files are the source of truth: delete one, re-import, and the row goes."""
    st = _store_with_docs(["x.md"])
    _put(st, "x", "x.md", "cto", B.STATE_DRAFT, evidence=["obs"])
    st.db.execute("DELETE FROM docs WHERE rel_path = 'x.md'")
    st.db.commit()
    assert st.belief_rows() == []
    assert st.belief_evidence_rows("x") == []
    st.close()


# ==========================================================================
# end-to-end through the real import path
# ==========================================================================


def _write_belief(d: Path, slug: str, **fm) -> Path:
    lines = ["---", f"name: {slug}", "type: belief"]
    for k, v in fm.items():
        if k == "evidence":
            lines.append("evidence:")
            lines += [f"  - '{x}'" for x in v]
        else:
            lines.append(f"{k}: {v}")
    lines += ["---", f"# {slug}", "body text about storage margins"]
    p = d / f"{slug}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _isolated_import(home: Path, source: Path) -> Path:
    """Run the REAL `lbrain import` against an isolated brain. Returns the db path.

    Goes through the command, not through `_project_belief`, because the
    behaviour under test IS import's branching. An earlier version of this test
    called the helper directly on both passes: it passed, and a mutation that
    deleted the call from the unchanged-body branch SURVIVED it. A test that
    cannot fail is not evidence.

    db_path is pinned into the isolated config on purpose. `Config`'s default
    resolves to the module-level DB_PATH captured at import time — i.e. the
    operator's real 200MB brain — so a test that let it default would import
    fixtures straight into the live install.
    """
    from click.testing import CliRunner

    from lbrain.cli import main

    db = home / "test-brain.db"
    (home / "config.toml").write_text(
        'embedding_provider = "local"\nembedding_model = "m"\nembedding_dim = 8\n'
        f'db_path = "{db.as_posix()}"\nsources = ["{source.as_posix()}"]\n',
        encoding="utf-8",
    )
    res = CliRunner().invoke(main, ["import", str(source)])
    assert res.exit_code == 0, res.output
    return db


def test_import_projects_beliefs_and_a_state_flip_alone_is_picked_up(isolate_lbrain_home):
    """The A-401 trap, applied to beliefs.

    A promotion edits ONLY frontmatter, so the body hash is unchanged and import
    takes the "unchanged" branch. If the projection were refreshed only on a body
    edit, the promotion would never reach retrieval — the gate would appear to do
    nothing and the belief would stay private forever.
    """
    home = isolate_lbrain_home
    d = Path(tempfile.mkdtemp())
    p = _write_belief(d, "margin", persona="cfo", state="draft", subject="storage",
                      claim="it inverts", created="2026-07-31", evidence=["[[obs]]"])

    db = _isolated_import(home, d)
    st = Store(db, embedding_dim=8)
    rel = next(iter(st.belief_states()))
    assert st.belief_states()[rel] == ("cfo", "draft")
    before = st.get_doc_hash(rel)
    st.close()

    # Promote by editing frontmatter only — the body must stay byte-identical.
    p.write_text(p.read_text(encoding="utf-8").replace("state: draft", "state: promoted"),
                 encoding="utf-8")
    _isolated_import(home, d)

    st = Store(db, embedding_dim=8)
    assert st.get_doc_hash(rel) == before, "precondition: the body did not change"
    assert st.belief_states()[rel] == ("cfo", "promoted")
    st.close()


def test_a_doc_that_stops_being_a_belief_loses_its_projection_on_reimport():
    from lbrain.cli import _project_belief

    d = Path(tempfile.mkdtemp())
    p = _write_belief(d, "temp", persona="cto", state="draft")
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    doc = parse(p, repo_root=d)
    with st.transaction():
        st.upsert_doc(doc)
        _project_belief(st, doc)
    assert st.belief_states()

    p.write_text(p.read_text(encoding="utf-8").replace("type: belief", "type: project"),
                 encoding="utf-8")
    doc2 = parse(p, repo_root=d)
    with st.transaction():
        st.upsert_doc(doc2)
        assert _project_belief(st, doc2) == 0
    assert st.belief_states() == {}
    st.close()


def test_broken_yaml_does_not_silently_unretract_a_belief(isolate_lbrain_home):
    """A-430, found by running the thing rather than reasoning about it.

    Malformed frontmatter makes `parse` return doc_type="" — which is exactly
    what an author removing `type: belief` looks like. Deleting the projection on
    that basis strips a RETRACTED belief of its burial and its marking, so a
    known-wrong record silently returns to competing on equal terms. Fail closed:
    "unreadable" is not "no longer a belief".
    """
    from lbrain.cli import _project_belief

    d = Path(tempfile.mkdtemp())
    p = _write_belief(d, "wrong", persona="cto", state="retracted", subject="s",
                      claim="c", created="2026-07-01")
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    doc = parse(p, repo_root=d)
    with st.transaction():
        st.upsert_doc(doc)
        _project_belief(st, doc)
    assert st.belief_states()[doc.rel_path] == ("cto", "retracted")

    # One bad indent — the exact shape of the live incident.
    p.write_text(p.read_text(encoding="utf-8").replace("subject: s", "  subject: s"),
                 encoding="utf-8")
    broken = parse(p, repo_root=d)
    assert broken.metadata_ok is False and broken.doc_type == ""  # precondition

    with st.transaction():
        st.upsert_doc(broken)
        _project_belief(st, broken)
    assert st.belief_states()[doc.rel_path] == ("cto", "retracted"), \
        "a retracted belief must not lose its burial to a YAML typo"
    st.close()


def test_a_genuinely_deleted_type_still_drops_the_projection(isolate_lbrain_home):
    """The fail-closed guard must not swallow the legitimate case: readable
    frontmatter that no longer says `type: belief` still clears the row."""
    from lbrain.cli import _project_belief

    d = Path(tempfile.mkdtemp())
    p = _write_belief(d, "gone", persona="cto", state="promoted")
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    doc = parse(p, repo_root=d)
    with st.transaction():
        st.upsert_doc(doc)
        _project_belief(st, doc)
    p.write_text(p.read_text(encoding="utf-8").replace("type: belief", "type: project"),
                 encoding="utf-8")
    doc2 = parse(p, repo_root=d)
    assert doc2.metadata_ok is True
    with st.transaction():
        st.upsert_doc(doc2)
        _project_belief(st, doc2)
    assert st.belief_states() == {}
    st.close()


def test_store_view_gates_a_real_belief_end_to_end():
    """Gate over a live Store, not a dict — the StoreCorpusView contract."""
    from lbrain.cli import _project_belief

    d = Path(tempfile.mkdtemp())
    obs = d / "turbo-credit.md"
    obs.write_text("---\nname: turbo-credit\ntype: project\n---\n6.6054 AR", encoding="utf-8")
    bel = _write_belief(d, "margin", persona="cfo", state="draft", subject="storage",
                        claim="it inverts", created="2026-07-31",
                        evidence=["[[turbo-credit]]"])
    st = Store(Path(tempfile.mkdtemp()) / "t.db", embedding_dim=8)
    with st.transaction():
        for p in (obs, bel):
            doc = parse(p, repo_root=d)
            st.upsert_doc(doc)
            _project_belief(st, doc)

    view = B.StoreCorpusView(st)
    res = B.gate(view.beliefs["margin"], view, today=TODAY)
    assert res.passed, res.report()
    assert res.roots == {"turbo-credit"}
    st.close()


# ==========================================================================
# serving — the reader must be told what it is reading
# ==========================================================================


def test_an_unprovisioned_home_warns_instead_of_answering_as_an_amnesiac(isolate_lbrain_home):
    """Closes the router lane's A-425.

    A persona is selected BY `LBRAIN_HOME`, so a typo used to mint a fresh empty
    brain and answer `docs: 0`, exit 0, silently — a specialist replaced by a
    confident amnesiac. Warn, do not refuse: a genuinely fresh install is the
    same state, and breaking first-run onboarding to catch a typo is worse.
    """
    from lbrain.cli import warn_if_unprovisioned

    msg = warn_if_unprovisioned()
    assert "UNPROVISIONED" in msg
    assert str(isolate_lbrain_home) in msg, "must name the home it actually resolved"

    (isolate_lbrain_home / "config.toml").write_text('sources = []\n', encoding="utf-8")
    assert warn_if_unprovisioned() == "", "a provisioned brain must stay silent"


def test_every_command_is_reachable_via_python_m():
    """A-429. The `if __name__ == "__main__"` dispatch sat mid-module at d58b45f,
    so main() ran before the rest of the file was executed and every command
    defined below it was absent under `python -m lbrain.cli` — `whoami`,
    `resolve`, the archive group. `lbrain <cmd>` worked, so the gap was invisible
    from the console script. Asserted through a real subprocess, because that is
    the only way the loading order is actually exercised.
    """
    import subprocess
    import sys as _sys

    from lbrain.cli import main as _main

    r = subprocess.run([_sys.executable, "-m", "lbrain.cli", "--help"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    missing = [c for c in _main.commands if c not in r.stdout]
    assert not missing, f"commands unreachable via `python -m lbrain.cli`: {missing}"


def test_a_draft_is_served_labelled_as_the_agents_own_output():
    from lbrain.serve import _header

    h = Hit(rel_path="cfo/hunch.md", chunk_idx=0, text="x", title="hunch",
            score=1.0, doc_type="belief")
    h.boosts["draft"] = 1.0
    out = _header(1, h, None)
    assert "NOT evidence" in out
    assert "type=belief" in out


def test_a_retracted_belief_is_served_marked_retracted():
    from lbrain.serve import _header

    h = Hit(rel_path="cto/wrong.md", chunk_idx=0, text="x", title="wrong",
            score=0.25, doc_type="belief")
    h.boosts["retracted"] = 0.25
    assert "RETRACTED" in _header(1, h, None)
