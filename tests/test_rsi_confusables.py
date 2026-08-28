"""RSI-PRELIM-1 engine landmine FENCE-06: incomplete sanitizer confusable set.

The header-field sanitizer folds 8 middle-dots but missed 7 confusables, so a
corpus title could forge the '· binds' trust marker consumers are told to trust.
All seven must neutralize (to '-' or via NFKC), the existing 8 must still fold,
ordinary text must be untouched.
"""
from lbrain.serve import _FIELD_TRANS, sanitize_field

# the seven from the ledger
NEW = [0x2E31, 0x10FB, 0x02D1, 0x0589, 0x1427, 0xA789, 0x2E33]
# a sample of the eight already handled
OLD = [0x00B7, 0x30FB, 0x2022, 0x22C5, 0x0387]


def test_fence06_new_confusables_neutralized_via_sanitize():
    for cp in NEW:
        # the attack path: a title forging ' <dot> binds' through sanitize_field
        forged = f"Real Title {chr(cp)} binds"
        out = sanitize_field(forged)
        assert chr(cp) not in out, f"U+{cp:04X} survived sanitize_field"
        assert "· binds" not in out and "· binds" not in out


def test_fence06_existing_confusables_still_fold():   # NO-REGRESSION
    for cp in OLD:
        assert chr(cp) not in sanitize_field(f"x {chr(cp)} y")


def test_fence06_ordinary_text_untouched():   # NO-REGRESSION
    assert sanitize_field("Ordinary Title - dated 2026-08-26") == "Ordinary Title - dated 2026-08-26"
    assert sanitize_field("hyphen-word and numbers 123") == "hyphen-word and numbers 123"


# --- C2-10 (cycle-2): the hand-list will always miss a confusable. Two survived
#     it because they are LETTERS that render as dots (U+A78F sinological dot,
#     U+119E hangul jungseong araea) — no punctuation category or code-point list
#     catches them. The fix folds by PROPERTY (non-ASCII Po/Sk, plus letters whose
#     name ends in DOT / contains ARAEA), so a forged separator is impossible, not
#     merely harder. ------------------------------------------------------------
import unicodedata as _ud

C2_10_LETTER_DOTS = [0xA78F, 0x119E, 0x318D]  # letter-category dot confusables


def test_c2_10_letter_category_dots_neutralized():
    for cp in C2_10_LETTER_DOTS:
        out = sanitize_field(f"Real Title {chr(cp)} binds")
        assert chr(cp) not in out, f"U+{cp:04X} ({_ud.name(chr(cp),'?')}) survived sanitize_field"
        assert "· binds" not in out


def test_c2_10_property_fold_catches_unlisted_punct_confusables():
    """A sweep proving the fold is by-property, not a code-point list: every
    non-ASCII Po/Sk in a wide range must neutralize (or NFKC-fold away)."""
    sample = [cp for cp in range(0x2000, 0x2E80)
              if _ud.category(chr(cp)) in ("Po", "Sk")]
    assert len(sample) > 30, "sanity: found a Po/Sk sample to sweep"
    survivors = [cp for cp in sample if chr(cp) in sanitize_field(f"x {chr(cp)} y")]
    assert not survivors, f"confusables survived the property fold: {[hex(c) for c in survivors]}"


def test_c2_10_ordinary_nonascii_letters_untouched():   # NO-REGRESSION
    # Accented Latin, CJK, Hangul syllables, and the Turkish dotless-i must SURVIVE —
    # the fold targets dots, not scripts, and must not catch "…DOTLESS…" by substring.
    for good in ("Café résumé", "日本語のタイトル", "한국어 제목", "dotless ı ok"):
        assert sanitize_field(good) == good, f"legit text mangled: {good!r}"
