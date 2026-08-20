"""Is the index current with respect to the SOURCES it was built from?

`doctor` verified the index against the CONFIG — stored vectors vs the live
embedding settings, the chunker fingerprint, inert config keys. It never
verified the index against the FILES on disk, so once the corpus changed —
files added, edited, superseded, renamed, deleted — `doctor` still reported
clean (issue #34, found on the experiment box 2026-08-11 while re-importing
after a corpus change).

That green light is what a user runs `doctor` to get when they ask *is my brain
current?*, and it could not answer that question. In the case that surfaced it,
four files were superseded or deleted in one regeneration; `doctor` would have
said clean either way, so it supplied no evidence the import had happened at
all. Same shape as reading a code default and reporting it as live behaviour:
a check that passes while the thing a reader believes it guarantees is false.

**This module does not invent a second definition of "current."** It asks
`import`'s own question — *would an import change anything?* — through
`import`'s own comparisons: the body hash first, then `doc_metadata_differs`
for the frontmatter edits `doc_hash` cannot see (A-401), then `prune_missing`'s
existence-AND-exclusion rule for the far side. A separate definition could
disagree with the importer, and a currency check that disagrees with the thing
that repairs currency is worse than no check: it either cries wolf forever or
clears while work remains.

Two guards are carried over deliberately, because the naive version of this
check is dangerous in exactly the way `prune_missing` already documents:

  - **An unmounted source root looks like every file vanished.** Reporting
    4,000 ORPHANED records because a drive is not mounted is a false alarm
    loud enough to train the operator to ignore the real one.
  - **Not looking is not the same as looking and finding nothing.** Docs under
    a missing root are counted as UNCHECKED and suppress the all-clear, rather
    than silently passing. A survey that skipped half the corpus and said
    "current" would be this very issue, re-committed inside its own fix.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .index import discover, is_backup_path, parse

# What an import would do to each divergent record. Named for the user's mental
# model ("what happened to my file?"), not the importer's branch names.
CHANGED = "CHANGED"        # body differs — chunks and vectors are stale
METADATA = "METADATA"      # frontmatter differs, body identical — one-row refresh
UNINDEXED = "UNINDEXED"    # on disk, never indexed
ORPHANED = "ORPHANED"      # indexed, no longer on disk (or now excluded)
UNREACHABLE = "UNREACHABLE"  # indexed, still on disk, but no configured source covers it


@dataclass
class Survey:
    """What `lbrain import` would do right now, without doing any of it."""

    on_disk: int = 0
    indexed: int = 0
    current: int = 0
    changed: list[str] = field(default_factory=list)
    metadata: list[str] = field(default_factory=list)
    unindexed: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    roots_missing: list[str] = field(default_factory=list)
    unchecked: int = 0
    elapsed: float = 0.0
    # False when nothing was surveyed at all (no sources configured). Distinct
    # from "surveyed and found nothing wrong" — see `is_current`.
    ran: bool = True

    @property
    def divergent(self) -> int:
        return (len(self.changed) + len(self.metadata)
                + len(self.unindexed) + len(self.orphaned))

    @property
    def is_current(self) -> bool:
        """True only when the index was FULLY surveyed and nothing diverged.

        A missing root, an unreadable file, or an unchecked doc all make this
        False. The alternative — reporting current because the divergence lists
        happen to be empty — is how a check earns trust it has not done the work
        to deserve.
        """
        return (self.ran and not self.divergent and not self.roots_missing
                and not self.unchecked and not self.unreadable)

    def counts(self) -> dict[str, int]:
        return {
            CHANGED: len(self.changed),
            METADATA: len(self.metadata),
            UNINDEXED: len(self.unindexed),
            ORPHANED: len(self.orphaned),
        }

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "is_current": self.is_current,
            "on_disk": self.on_disk,
            "indexed": self.indexed,
            "current": self.current,
            "changed": self.changed,
            "metadata": self.metadata,
            "unindexed": self.unindexed,
            "orphaned": self.orphaned,
            "unreachable": self.unreachable,
            "unreadable": self.unreadable,
            "roots_missing": self.roots_missing,
            "unchecked": self.unchecked,
            "elapsed": round(self.elapsed, 3),
        }


def survey(store, sources) -> Survey:
    """Compare every indexed doc against its source file, and vice versa.

    Exact, not heuristic: every discovered file is parsed and hashed. An mtime
    comparison would be cheaper and is tempting at ~1.4 ms/file, but mtime is
    metadata an archiver can restore — and a fast check that can report clean on
    a changed file is the bug this module exists to fix, not a trade-off to make
    inside its own implementation.
    """
    t0 = time.monotonic()
    s = Survey()
    roots = [Path(p).expanduser().resolve() for p in sources]
    if not roots:
        s.ran = False
        s.elapsed = time.monotonic() - t0
        return s

    missing = [r for r in roots if not r.is_dir()]
    s.roots_missing = [str(r) for r in missing]

    rows = store.doc_paths()
    s.indexed = len(rows)

    # rel_path is computed relative to the root that offered the file, exactly as
    # `import` does it — so the keys compared here are the keys the importer
    # would write. Iterating per-root rather than passing every root to
    # discover() at once is what preserves that.
    seen: set[str] = set()
    for src in roots:
        for path in discover([src]):
            try:
                doc = parse(path, repo_root=src)
            except Exception as e:  # unreadable is a finding, not a crash
                s.unreadable.append(f"{path}: {e}")
                continue
            s.on_disk += 1
            seen.add(doc.rel_path)
            existing = store.get_doc_hash(doc.rel_path)
            if existing is None:
                s.unindexed.append(doc.rel_path)
            elif existing != doc.doc_hash:
                s.changed.append(doc.rel_path)
            elif store.doc_metadata_differs(doc):
                s.metadata.append(doc.rel_path)
            else:
                s.current += 1

    for rel_path, abs_path in rows:
        if rel_path in seen:
            continue
        p = Path(abs_path)
        # A doc under a root that is not mounted was NOT surveyed. Calling it
        # orphaned would be the unmounted-drive false alarm `prune_missing`
        # already refuses to make; calling it current would be a lie of omission.
        if any(_under(p, m) for m in missing):
            s.unchecked += 1
            continue
        # Matches prune_missing exactly: "no longer indexable" is not the same as
        # "no longer on disk" — a doc that moved into a backup tree still exists
        # and would still be served forever without this second clause.
        if not os.path.exists(abs_path) or is_backup_path(p):
            s.orphaned.append(rel_path)
        else:
            # Still on disk, but discover() under the configured roots never
            # produced this key: the source was dropped from `sources`, or the
            # row was written under a different repo_root by `lbrain import
            # <subdir>`. Either way no import will ever refresh it and no prune
            # will ever remove it — it is served, indefinitely, unmaintained.
            s.unreachable.append(rel_path)

    s.elapsed = time.monotonic() - t0
    return s


def _under(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except OSError:
        return False
