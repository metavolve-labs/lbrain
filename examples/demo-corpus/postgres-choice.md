---
date: 2026-05-20
type: decision
---
We picked Postgres over SQLite for the API tier: concurrent writers and row-level
locking mattered more than zero-ops. Revisit if the API tier ever goes single-writer.
