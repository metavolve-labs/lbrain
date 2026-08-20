# Releasing lbrain — the checklist is the gate

**Standing rule (Tad, 2026-08-19): every version push runs this. No green, no
publish.** This exists because 0.1.5 shipped with its internal `__version__`
still reading 0.1.4, and without a module a downstream repo imports — both
caught by other machines after the fact instead of by the release before it.

## The short version

```bash
# on main, clean tree, everything pushed:
bash scripts/release_gate.sh        # ⛔ red → fix; ✅ green → the next line
git tag vX.Y.Z && git push origin vX.Y.Z && twine upload dist/*
```

## What the gate measures (each check is a shipped failure)

| # | Check | The incident it prevents |
|---|---|---|
| 1 | `pyproject.toml` version == `lbrain/__init__.py` `__version__` | 0.1.5 wheel answering `0.1.4` at runtime |
| 2 | clean tree, on `main`, HEAD pushed to `origin/main` | releasing code no other machine can see (hazard #10) |
| 3 | wheel module inventory == source module inventory | packaging silently dropping a module |
| 4 | fresh-venv install: version, every module imports, CLI answers | "works on my machine" == works from the artifact |
| 5 | `release-consumers.txt` contracts import against the wheel | 0.1.5 shipping without `lbrain.grading` while lbrain-teams pins `>=0.1.5` and imports it |

## After publishing — verify from the outside, then announce

1. Wait for the index, then in a **fresh venv**: `pip install lbrain==X.Y.Z`,
   confirm `python -c "import lbrain; print(lbrain.__version__)"` prints X.Y.Z,
   and re-run the consumer imports.
2. Announce in the channel **with the measured output pasted**, not a claim.
   "Published 0.1.6" is an assertion; the venv transcript is a receipt.

## Maintaining `release-consumers.txt`

One import statement per line — the promises other repos build on. When a
downstream repo starts importing something new from lbrain, its PR adds the
line here **in the same change**. The gate then keeps the promise for every
future release.

## When the gate is wrong

If a check misfires, fix the *gate* in the same PR that works around it —
a bypassed gate teaches everyone the gate is optional, which is the one
lesson this file exists to prevent.
