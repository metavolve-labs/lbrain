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
