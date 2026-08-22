# Demo corpus

Six small notes for trying LBrain without pointing it at your own files. Two disagree
about a deploy flag and the newer one supersedes the older. Two mention Redis and
caching without ever deciding an eviction policy. One is about lunch.

```
lbrain init --source examples/demo-corpus
lbrain import && lbrain embed --stale
```

Query 1 — supersession:

```
lbrain query "what flag do deploys use?"
```

Expected: the August note ranks first and binds; the March note is demoted and flagged
SUPERSEDED; everything else shows near-miss.

Query 2 — abstention:

```
lbrain query "what is our production Redis eviction policy?"
```

Expected: every match comes back near-miss. Text about Redis exists; a decision about
eviction policy does not, and nothing pretends otherwise.
