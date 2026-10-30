# Identity by realpath: one file is one doc, and the publish line equals the docs table

Branch: `fix/realpath-dedupe-2026-10-30` (not pushed; the CTO pushes).
Report: Prism seat, workhorse, 2026-10-30T10:49Z.

## Problem

Prism's repo path `/home/metavolve-labs/golden_codex_pipeline` is a symlink to
`/home/metavolve-labs/golden`. Two source roots were listed in `config.toml` under the
symlink spelling, while the A-563 baseline sources add the same roots under the real
spelling. The engine keyed documents by the path as written, so:

1. 121 documents were indexed twice, one row per spelling, with 116 to 120 duplicate
   `doc_hash` values. The top three of a doctrine query showed the same document twice.
2. `lbrain epoch build --prune-unreachable` removed nothing. The reachability guard
   (`store._under_roots`) already resolves the real path, so the alias row counted as
   reachable, while import had recorded the alias spelling as a document of its own.
   MS-01 (`resolve_rel_path`) then saw a bare key held by a "different" live file and
   minted the disambiguated `<src_name>/<rel_path>` twin, by design.
3. The publish line printed `docs: 871` while the published `brain.db` docs table held
   992 rows. The publish figure summed a per-source inventory matched by byte prefix on
   `abs_path`; rows stored under the symlink spelling fell outside every configured
   prefix and were simply not counted. The engine disagreed with its own database.

Prior design this change follows rather than contradicts (found with `lbrain search
"realpath"` and `lbrain query "symlink alias docs indexed twice"`, LBRAIN_HOME=/root/.lbrain):

- `store._norm_path` (realpath + normcase; CSO/mac review of PR #57, 2026-09-07) is the
  rule the reachability guard and `_unscanned_count` already apply. This PR extends the
  same rule to the identity of a row and to the inventories, so every root test in the
  engine is judged on the same real path.
- A-626: build manifests name real source roots.
- Anomaly Register entry of 2026-09-22 (workhorse CTO): "identity by spelling, not by
  inode ... the engine should canonicalise source roots (realpath) before keying docs".
- Keeper seat self-analysis, 2026-10-15, finding A3: 11 ghost alias rows under
  `/home/metavolve-labs/keeper` (a symlink); recommendation "canonicalise paths at ingest".
- The 2026-07-28 red-team finding 2 (symlinked files escaping the corpus) is untouched:
  `discover()` still refuses a path that resolves outside the root that offered it, before
  anything here runs.

## Change

Identity is the real path (inode-equivalent), never the spelling. Content is not identity.

### Import (`lbrain/cli.py`, `lbrain/store.py`)

- `Store.upsert_doc` stores `abs_path = os.path.realpath(path)`.
- `Store.resolve_rel_path` canonicalises the path it is asked about before any lookup, so
  a file offered through a symlinked root or a second overlapping root resolves to the row
  that already holds it, instead of minting a disambiguated twin.
- The import loop keeps a per-scan map realpath -> rel_path. The first configured root that
  reaches a file owns its row and its `rel_path`; every later offer of the same real file
  is an alias: not parsed, not counted as new/updated/unchanged, recorded under the
  metadata key `lbrain_path_aliases` on the owning row (`Store.record_path_aliases`, inside
  the scan's transaction; a stale alias record is dropped when the alias root leaves the
  config). `Store.doc_metadata_differs` ignores that key, so a stable alias set never reads
  as a frontmatter change.
- `Store.canonicalise_abs_paths` runs at the start of every import and rewrites any stored
  `abs_path` to its realpath. Rows written before this change (or through a symlink
  introduced later) become equal-`abs_path` twins, which is exactly the shape DD-01
  (`dedupe_identity`, 2026-08-31) already collapses: the row the scan refreshed stays, the
  spelling twin and its chunks, vectors, wikilinks, supersessions and claim spans go.
  Nothing is deleted by the canonicalisation itself.
- The import summary gains `aliases: N` and `paths canonicalised: N` when non-zero.
- `index_currency.survey` skips a file whose realpath it has already surveyed, so `doctor`
  counts what `import` counts (the module's own rule: never a second definition of current).

### Epoch build (`lbrain/epoch_build.py`)

- `_inventory`, `_inventory_abs` and `_purge_source` match a row to its configured source
  on real paths both sides (`_root_matcher`, built on `store._norm_path`). Inventory keys
  stay the configured spelling, so watermark digests and the deletion manifest are still
  keyed by what `config.toml` says.
- The source-collapse gate counts distinct real paths, not rows. Without this, the first
  build that collapses a root's spelling twins would expect twice the files the root holds
  and refuse itself as a collapse.
- The publish line, `report["docs"]` and `build-manifest.json: docs` are now the
  candidate's own `SELECT COUNT(*) FROM docs`, the figure the published file is verified
  against. The per-source sum is kept beside it as `docs_in_sources`; the two differ only
  when rows lie under no configured root (already reported as `coverage_unscanned`), never
  because of how a root was spelled.

### Not changed

- Schema. `rel_path` stays the primary key; `abs_path` is a value. No migration script.
- `prune_unreachable` and `_unscanned_count`: they already used the realpath rule; the
  import side now agrees with them, which is what makes the publish count equal the table.
- `discover()` symlink-escape refusal (red-team 2026-07-28).

## Tests

Baseline on `main` (140fc9d), `LBRAIN_HOME=$(mktemp -d) python -m pytest tests`:
4924 passed, 2 skipped, 2 xfailed (17m51s).

After the change, same command on this branch: 4931 passed, 2 skipped, 2 xfailed (13m51s).
That is the baseline plus the 7 new tests; no test removed, none marked xfail, none loosened.

New file `tests/test_realpath_dedupe_symlink_roots.py` (7 tests, POSIX symlinks):

- symlinked spelling first, real spelling second, a wider containing root third: one doc
  per file, zero duplicate `doc_hash`, `rel_path` from the first root, `abs_path` is the
  realpath, alias spelling recorded in metadata, summary reports aliases.
- the summary counts the alias visits once (`new: 2`, then `unchanged: 2`).
- `--prune-unreachable` leaves the canonical doc; the dry run is empty; every stored
  `abs_path` is under a root by the guard's own `_under_roots`.
- a brain with the historical twin rows (planted by SQL as the old engine wrote them)
  collapses to one row on one import, `identity-dupes collapsed: 1`, zero duplicate hashes.
- the alias record is dropped when the alias root leaves the config, and a stable alias
  set does not read as `meta-refreshed`.
- negative control: two different files with identical content under two roots remain two
  docs (same hash, different realpath, no alias record). Content is not identity.
- end to end through `epoch_build.build(prune_unreachable=True)` on a legacy brain
  carrying a planted twin: `report["docs"] == manifest["docs"] == manifest["docs_in_sources"]
  == SELECT COUNT(*) FROM docs == 2`, zero duplicate hashes.

Adjacent suites rerun green with no change: dedupe identity (DD-01), multisource rel_path
collision (MS-01), prune unreachable (both), prune scope, prune guards, epoch source
collapse, import reconcile A-591 and 5B gap closures (including the W6 symlink-spelling
cases), index currency, security red-team 2026-07-28, single-file roots, exclude markers,
whoami counts parity: 148 passed, 2 skipped.

## Migration note

Existing brains need one rebuild to dedupe. Run the normal `lbrain epoch build` (a delta
build is enough; `--full` is not required). The import stage canonicalises every stored
`abs_path`, DD-01 collapses the spelling twins, and the gate counts distinct real paths so
the collapsing build is not refused as a source collapse. Legacy (non-epoch) homes get the
same on the next `lbrain import`.

No data loss: the kept row is the one the scan refreshed from the file on disk; the alias
spelling is written into its metadata. The removed row is a duplicate of a file that is
still indexed. Embedding reuse (`embed --reuse-from`) is keyed by chunk hash, so the
collapse does not re-embed anything.

Config hygiene, separate from this fix: a source list that names a symlink and its target
is still a config error worth cleaning (2026-09-22 anomaly). After this change it is a
harmless one; the alias is recorded, not indexed.

Operators reading `build-manifest.json`: `docs` now equals the table count; the former
per-source sum is `docs_in_sources`.

## Rollback

`git revert` of the single commit (or do not merge the branch). The schema is unchanged,
so an epoch built by this code is readable by the previous code. A brain that already went
through one import on this code keeps realpath `abs_path` values and no twin rows, both of
which the previous code reads correctly (its reachability guard already resolved real
paths); it would simply start re-minting twins on the next import under an overlapping
config. The previous epoch stays on disk as the rollback target under the usual
`epoch prune` keep policy.
