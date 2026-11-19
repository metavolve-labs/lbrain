# FIX-REPORT: captured sessions reachable through ordinary recall (2026-11-19)

Branch `cto/capture-reachable-through-recall-2026-11-19` (worktree /root/lbrain-fix-capture). Not pushed.

## Design
1. `index_captured_snapshots` (default false). When true, `lbrain epoch build` writes one document per SERVED captured
   session (archived, not shredded, newest capture of its session) to `<home>/captures/captured-session-<session>.md`.
2. Per the CSO change (03:04Z) the document holds the FULL text of the session's user/assistant turns, not the snapshot:
   rendered by the archiver's own transcript renderer (thinking, tool inputs, tool results dropped; 1 MB cap of turns).
3. Plaintext source: the staged spool payload during the sweep (sha256-verified, before the post-publish drain); for
   a capture already drained, the home's archived record decrypted with the home's key + the build's passphrase.
4. Every line goes through `identity.redact_secrets` (the existing `_SECRET_PATTERNS`, applied line by line).
5. Lines the index would read as corpus declarations (column-0 `Supersedes:`, retirement banners, `**Last Updated**:`,
   code fences) are prefixed `~ `, so a conversation cannot bury, retire or re-date anything.
6. Frontmatter: name `captured session <id> at <time>`, `type`/`kind: captured-session`, `date`, `captured_at`,
   `session_id`, `txid`, `namespace`, `transcript_path`. `serve.DOC_TYPES` gains `captured-session`, so the record header
   reads `type=captured-session · dated <capture date>` inside the usual untrusted-data fence.
7. The captures root becomes a source (Config.load appends it when the switch is on and the dir exists; never written
   back to config.toml); the build adds it to its own source list, so the inventory, deletion manifest, collapse check
   and coverage all cover it. After the sweep: narrow `import --prune --force-prune <root>` + `embed --stale`.
8. Switch turned off again: the next build purges the documents from the candidate (files stay on disk).
9. `recall_preview_chars` (default 300) replaces the three `[:300]` slices; `lbrain recall --chars N` overrides once.
10. `lbrain capture-pointer`: one line per session captured in the last 24 h (state, chunk count, five terms by
    tf x BM25-idf over `admissibility._terms` tokens, idf from the epoch's own `fts_chunks`); screened.

## CSO security read of 8c706b9: what changed (second commit)
- **Defect 1, exclude bypass.** A capture with no recorded `source_path` (every capture staged before this change) is
  SKIPPED (`skipped_no_path` in the report) unless `lbrain epoch build --backfill-captures-without-path` is passed; then
  each indexed txid is listed under `backfilled`. The flag refuses to combine with `--wait-for-builder` (a follower
  could otherwise be satisfied by a build that did not backfill). The build CLI now prints the `captured_docs` report.
- **Defect 2, shred twin.** `lbrain shred --txid T` unlinks the captured-session document whose frontmatter txid is T
  in the SAME call, before anything that can refuse, and writes a tombstone `<root>/.shredded/T` (an empty file named
  by txid; no content) so no build writes it back. On an epoch home the KEY shred still refuses (unchanged; the
  existing limitation), so on an epoch home the call removes the plaintext twin and exits non-zero for the archive.
  The next build drops the rows by name (`index_rows_dropped`); those drops (and the writer's own removals) are
  credited as intended deletions for the captures root only, so the deletion manifest and collapse check still judge
  every other loss. A tombstone covers that txid only: a NEWER capture of the same session (a superset of its text)
  keeps its own document until it too is shredded.
- **Switch off** now deletes `captured-session-*.md` from disk as well as purging the rows; the report lists
  `files_deleted_switch_off` and `remaining_under_root` (the tombstone directory, if any shred happened, and any file
  that is not a captured-session document).
- **Condition 4.** `index.parse` mints no wikilink or supersession edges for `type`/`kind: captured-session`;
  `serve.record_date` dates such a hit by its frontmatter capture date before the ladder runs, and
  `staleness.claim_date` (raw text, `lbrain stale`) does the same when the frontmatter block says `kind: captured-session`.
- **Condition 5.** The full text is rendered uncapped, then `keep_newest` keeps the NEWEST 1,000,000 characters, cut to
  a turn boundary, with a marker saying older turns were omitted.
- **Condition 6.** `redact_secrets` now redacts a whole PEM private-key block (header to END line, or to the first
  non-PEM line when unterminated); the header shape is the one `_SECRET_PATTERNS` already names. A test plants one
  sample per `_SECRET_PATTERNS` entry (asserting each sample exercises its own pattern) plus a multi-line RSA key and
  asserts none survives `full_text` -> `render_doc`.
- **Condition 3 (policy).** A home shared by several people stays OFF: ordinary recall has no per-owner filter, so ON
  would serve one person's session text to everyone who queries that home.
- **Remote embedding provider.** On `gemini` / `openai`, ON sends the screened whole-session text to that provider to
  be embedded (stated in the config key's comment).

## CSO re-read of b5f76c6 (04:02Z): what changed (third commit)
- **Shred output on an epoch home** (ordering unchanged: plaintext first, then the key shred refuses, exit non-zero):
  (a) `plaintext captured-session document for T removed for good: <path> (tombstoned at <root>/.shredded/T; no build
  writes it back). The encrypted record, its key and its `lbrain recall` snapshot REMAIN: the archive key shred is
  refused on an epoch home.` (b) `to complete the shred on this epoch home, run: [shred -u <spool payload> && ]shred -u
  <home>/keys/T.key && lbrain epoch build` -- the build's existing keyless-receipt heal then marks the record shredded
  in the new epoch (snapshot, FTS, vectors, chunk vectors purged) and drops the false receipt; a payload still in the
  spool is removed first, or the heal would re-capture it under a new key. Tested end to end: after the printed steps
  and a build the archive row is `shredded = 1` with an empty snapshot and `lbrain retrieve` fails. A record with no
  spool receipt gets the key step only, and the line says no build purges its snapshot in this engine version. When
  another SERVED capture of the same session exists, a third line names its txid (tested). Retained prior epochs keep
  their copy until pruned.
- **Claims, contradictions, supersession (the receipt question).** Every path by which a document can act on OTHER
  records was checked: supersession edges (frontmatter `supersedes:` and body `Supersedes:` lines; the only mechanism
  that buries another record), wikilinks (graph), claim spans (frontmatter `claims:`; they only ever filter the
  declaring document's own chunks under `--current-only`), retirement banners and `status:` (self only), beliefs and
  the G5 contradiction gate (only `type: belief` documents), `lair_check_action` (only `type: feedback` documents and
  core rules). A `captured-session` document now mints no supersession edges, no wikilinks and no claim spans at parse
  time, whatever its frontmatter says; banner-shaped and `Supersedes:` lines in the conversation are neutralised, so it
  cannot retire itself either. Test: ten planted contradiction-shaped lines (SUPERSEDED/RETIRED banners, `Supersedes:`
  both forms, `status: retired`, a `claims:` block naming a.md's text as retracted, a CONTRADICTS line, a `Last
  Updated` header) mint zero rows in claim_spans, supersessions and wikilinks; a.md stays current and unmarked under
  `--current-only`; plus a parse-level test on hand-written frontmatter carrying `supersedes:` and `claims:`.
- **Backfill needs the list.** `--backfill-captures-without-path SESSIONS` (comma-separated ids or `@FILE`, one per
  line, `#` comments) is required to name the sessions expected; the bare flag or an empty list refuses (exit 2). A
  path-less capture not on the list is reported under `not_on_backfill_list` and stays unindexed; a listed session
  not found is reported under `backfill_expected_not_found`; a document an earlier backfill wrote is kept by later
  builds (`kept_backfilled`).
