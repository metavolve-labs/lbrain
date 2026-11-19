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
