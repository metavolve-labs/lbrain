# Demo corpus

Four small notes for trying LBrain without pointing it at your own files yet. Two of
them disagree about a deploy flag; the newer one supersedes the older; two are about
other things entirely.

```
lbrain init --source examples/demo-corpus
lbrain import && lbrain embed --stale
lbrain query "what flag do deploys use?"
```

Expected: the August note ranks first and binds; the March note is demoted and flagged
SUPERSEDED; the Postgres and lunch notes show near-miss because they don't answer the
question. That's the product in one query.
