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

---

## 2026-09-02 addendum — the preflight layer (Tad: "slow it down")

Two more misfires shipped **without running the gate above**: 0.1.9/0.1.9.post1 (published
over red CI, from a branch, without the fixes their PR named) and 0.1.10 (self-reporting
0.1.9.post1 — the drift test was RED in CI and the publish path never read it). The gate
was right all along; the failure was that nothing FORCED it into the publish path.

**`scripts/release-preflight.sh` is now the one command, and it runs the gate first:**

```bash
scripts/release-preflight.sh --pre-verdict            # gate + new checks + build; prints
                                                      # dist/ hashes for the CSO workorder
scripts/release-preflight.sh --cso-verdict <ref>      # the release authorization
```

What it adds on top of the gate (each a 2026-09-01/02 scar):
- **CI conclusion == success on THIS exact sha** (both misfires published past red CI —
  the gate can't see CI; this can).
- **RELEASE-REQUIRES ancestry** (0.1.9 shipped without the fixes its PR named).
- **Full suite warm AND forced-cold** (`LBRAIN_TEST_FORCE_COLD=1` — two days of red cold
  CI read as background noise until a real red hid inside it).
- **The built wheel agrees with ITSELF** (`__version__` read out of the wheel bytes vs
  metadata) **and all three installed surfaces match** (`__version__`, `--version`,
  metadata — 0.1.10 shipped with one right surface and two wrong ones, and verification
  checked the right one).
- **`--cso-verdict` is a required argument** — an independent artifact verdict is an INPUT
  to a release (VD-01); the verdict reference goes into the tag message, and release.yml
  refuses a tag whose message doesn't name one.

Dispatch release.yml **at the tag**, watch the run to its conclusion, then verify from
PyPI in a fresh venv before any announcement — the "After publishing" section above, now
with `--version` explicitly in the list of surfaces to check.
