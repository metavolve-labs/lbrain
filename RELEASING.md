# Releasing LBrain

One page, because the 0.1.4 release failed once for a missing step that lived only in a
workflow error message. The workflow (`.github/workflows/release.yml`) is dispatch-only by
design — publishing is approval-by-initiation — and it enforces two guards:

1. the `pyproject.toml` version must **not** already exist on PyPI (a version is burned
   forever on upload), and
2. a git tag `v<version>` must exist and point at the commit being published.

**Use the script; it performs the checks in order and cannot forget the tag:**

```bash
scripts/release.sh          # checks, tags, dispatches, verifies — stops loudly on any gap
```

What it enforces, in order: clean tree on up-to-date `main` · `pyproject.toml` and
`lbrain/__init__.py` agree on the version · a `CHANGELOG.md` stanza for that version
exists · version absent from PyPI · tag `v<version>` created on `main` HEAD and pushed ·
workflow dispatched · run polled to success · PyPI polled until it serves the new version.

Manual path (if the script can't run): bump both version strings → changelog stanza →
merge to main → `git tag -a v<X.Y.Z> <main-sha> && git push upstream v<X.Y.Z>` →
`gh workflow run release.yml` → verify `https://pypi.org/pypi/lbrain/json` serves it →
clean-venv install test.

The user-facing upgrade contract this must never break: `docs/UPGRADING.md`.
