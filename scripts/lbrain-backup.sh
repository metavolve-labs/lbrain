#!/usr/bin/env bash
# lbrain-backup.sh [DEST] — the documented backup route (docs/BACKUP-AND-RESTORE.md), for BOTH home shapes.
#
# CCO A5 independent replay, 2026-09-11T07:40Z: the guide's inline backup block passed B1-B4 on an
# epoch-managed home and exited 1 on a valid LEGACY home (root brain.db, no epochs/ tree), leaving a
# config-only "backup". A route that works on one of the two shapes the product ships is half a route.
# This script detects the shape, copies what a restore needs, checks each database copy, and writes a
# manifest saying what it did. Exit codes: 0 done and verified · 2 the home is torn or a writer is live
# (nothing copied) · 3 a copied database failed its integrity check (copy kept, marked FAILED).
set -u
H="${LBRAIN_HOME:-$HOME/.lbrain}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
B="${1:-$H/backups/home-$TS}"
[ -d "$H" ] || { echo "lbrain-backup: no home at $H" >&2; exit 2; }
command -v sqlite3 >/dev/null || { echo "lbrain-backup: sqlite3 is required for a consistent copy" >&2; exit 2; }

# Quiescence, judged from THIS home and not from the process table: a first version used `pgrep` for any
# lbrain writer on the machine, which refused on an unrelated home's build and is not a property of the
# home being copied. An epoch home's writers (`epoch build`, `epoch prune`) hold the builder lock
# `epochs/.builder.lock.d` (one builder per home, epoch.py BuilderLock); a `.building` directory means a build
# is live or died mid-way. A legacy home's writers (`import`, `embed`, `capture`) go through SQLite, and
# the online backup API below yields a consistent snapshot regardless, so no process check is needed there.
if [ -e "$H/epochs/.builder.lock.d" ]; then
  echo "lbrain-backup: $H/epochs/.builder.lock.d exists; a build or prune holds this home. Wait for it. Not copying." >&2; exit 2
fi
if ls -d "$H"/epochs/*.building >/dev/null 2>&1; then
  echo "lbrain-backup: $H/epochs has a .building directory; a build is live or died mid-way. Not copying." >&2; exit 2
fi

mkdir -p "$B" || exit 2
MAN="$B/BACKUP-MANIFEST.txt"
{ echo "lbrain-backup $TS"; echo "source_home=$H"; } > "$MAN"
for f in config.toml identity.json CORE.md env; do
  [ -f "$H/$f" ] && cp -a "$H/$f" "$B/$f" && echo "copied=$f" >> "$MAN"
done

check() { # check <db> → 0 if PRAGMA quick_check says ok
  [ "$(sqlite3 "$1" 'PRAGMA quick_check;' 2>/dev/null | head -1)" = "ok" ]; }

if [ -f "$H/epochs/CURRENT" ]; then
  EID=$(tr -d '[:space:]' < "$H/epochs/CURRENT")
  if [ -z "$EID" ] || [ ! -f "$H/epochs/$EID/brain.db" ]; then
    echo "lbrain-backup: $H/epochs/CURRENT names '$EID' but its brain.db is missing: the home is TORN. Not copying." >&2
    echo "shape=epoch status=TORN current=$EID" >> "$MAN"; exit 2
  fi
  cp -a "$H/epochs" "$B/epochs" || { echo "lbrain-backup: copying epochs/ failed" >&2; exit 2; }
  echo "shape=epoch current=$EID" >> "$MAN"
  if check "$B/epochs/$EID/brain.db"; then echo "quick_check=ok db=epochs/$EID/brain.db" >> "$MAN"
  else echo "quick_check=FAILED db=epochs/$EID/brain.db" >> "$MAN"; echo "lbrain-backup: copied CURRENT db failed quick_check" >&2; exit 3; fi
  echo "sha256=$(sha256sum "$B/epochs/$EID/brain.db" | cut -d' ' -f1) db=epochs/$EID/brain.db" >> "$MAN"
else
  # Legacy shape: one database at the CONFIGURED path (config.toml db_path), default $H/brain.db.
  DB=$(sed -n 's/^db_path *= *"\(.*\)"/\1/p' "$H/config.toml" 2>/dev/null | head -1)
  DB="${DB/#\~/$HOME}"; [ -n "$DB" ] || DB="$H/brain.db"
  [ -f "$DB" ] || { echo "lbrain-backup: legacy home but no database at $DB (config.toml db_path or the default)" >&2
                    echo "shape=legacy status=NO-DB db_path=$DB" >> "$MAN"; exit 2; }
  # sqlite3's online backup API takes a consistent snapshot even with -wal/-shm present; cp of a live
  # WAL database can miss committed pages. The copy is always named brain.db in the backup.
  sqlite3 "$DB" ".backup '$B/brain.db'" || { echo "lbrain-backup: sqlite .backup failed" >&2; exit 2; }
  echo "shape=legacy db_path=$DB" >> "$MAN"
  if check "$B/brain.db"; then echo "quick_check=ok db=brain.db" >> "$MAN"
  else echo "quick_check=FAILED db=brain.db" >> "$MAN"; echo "lbrain-backup: copied db failed quick_check" >&2; exit 3; fi
  echo "sha256=$(sha256sum "$B/brain.db" | cut -d' ' -f1) db=brain.db" >> "$MAN"
fi
echo "lbrain-backup: wrote $B"; sed 's/^/  /' "$MAN"
echo "lbrain-backup: prove it readable before you need it:"
if grep -q '^shape=epoch' "$MAN"; then
  echo "  LBRAIN_HOME='$B' lbrain epoch status"
else
  echo "  (legacy shape: point a scratch home's config.toml db_path at $B/brain.db, or restore first)"
fi
echo "  LBRAIN_HOME='$B' lbrain search '<a phrase you know is in your corpus>'"
exit 0
