"""Admissibility gate rung-1 unit tests (behavioral contract)."""
from lbrain.admissibility import judge, qtype


def test_qtype():
    assert qtype("How many rows did the ledger record?") == "quantity"
    assert qtype("When is the filing due?") == "date"
    assert qtype("Which revision was promoted?") == "identity"


def test_admissible_binds_specific():
    rec = "The backfill ledger recorded 198 rows, 99 partial_upload plus 99 done."
    v = judge("How many rows did the backfill ledger record?", rec)
    assert v.verdict == "ADMISSIBLE" and any("198" in c for c in v.bound_candidates)


def test_near_trap_wrong_entity_number():
    # the "924 for 780" trap: right-shaped number, wrong dossier
    rec = ("The 0x_b1ank dossier is preparing for inscription. Of the career "
           "budget, 924 remain reserved beyond the first release of Contraband pieces.")
    v = judge("How many career works are reserved beyond Genesis for ViGOR?", rec)
    assert v.verdict == "INADMISSIBLE_NEAR"


def test_irrelevant():
    rec = "Columnar basalt forms by thermal contraction as lava cools slowly."
    v = judge("Which register-api revision was promoted to 100% traffic?", rec)
    assert v.verdict == "IRRELEVANT"


def test_diacritic_entity_anchor():
    recA = "The VīGOR dossier stands at an Edition of 880 with 780 reserved."
    recB = "The Artiswa dossier is capped at 1111 with 1,011 reserved for milestones."
    q = "How many works are reserved in the VīGOR dossier?"
    assert judge(q, recA).verdict == "ADMISSIBLE"
    assert judge(q, recB).verdict in ("INADMISSIBLE_NEAR", "IRRELEVANT")
