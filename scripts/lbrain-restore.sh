#!/usr/bin/env bash
# lbrain-restore.sh <BACKUP_DIR> — the documented Route 1 restore (docs/BACKUP-AND-RESTORE.md), both home shapes.
#
# CCO independent replay, 2026-09-11T07:53Z, on the guide's inline legacy block: it resolved db_path from the
# CURRENT config, put the database there, then restored the OLDER config pointing elsewhere; the search
# found no records and every command returned 0. A restore that reports success while config and database
# disagree is the failure this script exists to end. Order here: config FIRST, then the database at the path
# the RESTORED config names, then verification against the backup's own manifest. Exit 0 only when the
# restored database's sha256 equals the manifest's and the config/database pair is consistent; 2 nothing
# restored (reason on stderr); 3 restored but verification failed (the wreck is kept, nothing deleted).
set -u
B="${1:-}"; H="${LBRAIN_HOME:-$HOME/.lbrain}"
[ -n "$B" ] && [ -d "$B" ] || { echo "lbrain-restore: usage: lbrain-restore.sh <backup dir>" >&2; exit 2; }
MAN="$B/BACKUP-MANIFEST.txt"
[ -f "$MAN" ] || { echo "lbrain-restore: $B has no BACKUP-MANIFEST.txt; this is not a lbrain-backup.sh backup" >&2; exit 2; }
SHAPE=$(sed -n 's/^shape=\([a-z]*\).*/\1/p' "$MAN" | head -1)
grep -q '^shape=.*status=' "$MAN" && { echo "lbrain-restore: the manifest records a refused backup ($(grep '^shape=' "$MAN")); nothing to restore" >&2; exit 2; }
WANT=$(sed -n 's/^sha256=\([0-9a-f]*\) db=.*/\1/p' "$MAN" | head -1)
[ -n "$WANT" ] || { echo "lbrain-restore: manifest carries no database sha256" >&2; exit 2; }
if [ -e "$H/epochs/.builder.lock.d" ]; then echo "lbrain-restore: a build or prune holds $H; wait for it" >&2; exit 2; fi
mkdir -p "$H" || exit 2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# 1. config and identity FIRST, so every path below is resolved from the restored state, never the old one
for f in config.toml identity.json CORE.md env; do
  if [ -f "$B/$f" ]; then
    [ -f "$H/$f" ] && cp -a "$H/$f" "$H/$f.pre-restore-$STAMP"
    cp -a "$B/$f" "$H/$f" || { echo "lbrain-restore: could not restore $f" >&2; exit 2; }
  fi
done

if [ "$SHAPE" = "epoch" ]; then
  # 2. the epochs tree: move the wreck aside, never delete it
  [ -d "$H/epochs" ] && mv "$H/epochs" "$H/epochs.broken-$STAMP"
  cp -a "$B/epochs" "$H/epochs" || { echo "lbrain-restore: copying epochs/ failed; the wreck is at $H/epochs.broken-$STAMP" >&2; exit 3; }
  EID=$(tr -d '[:space:]' < "$H/epochs/CURRENT"); DB="$H/epochs/$EID/brain.db"
elif [ "$SHAPE" = "legacy" ]; then
  # 2. the database at the path the RESTORED config names (default <home>/brain.db), not the path the old one named
  DB=$(sed -n 's/^db_path *= *"\(.*\)"/\1/p' "$H/config.toml" 2>/dev/null | head -1); DB="${DB/#\~/$HOME}"; [ -n "$DB" ] || DB="$H/brain.db"
  mkdir -p "$(dirname "$DB")"
  [ -f "$DB" ] && mv "$DB" "$DB.broken-$STAMP"
  rm -f "$DB-wal" "$DB-shm"    # stale WAL/shm belong to the old file, not to the restored one
  cp -a "$B/brain.db" "$DB" || { echo "lbrain-restore: copying brain.db failed" >&2; exit 3; }
else
  echo "lbrain-restore: manifest shape '$SHAPE' unknown" >&2; exit 2
fi

# 3. verify the pair: the database the restored config resolves to is byte-identical to what the backup recorded
GOT=$(sha256sum "$DB" | cut -d' ' -f1)
if [ "$GOT" != "$WANT" ]; then
  echo "lbrain-restore: VERIFICATION FAILED: restored db at $DB has sha256 $GOT, manifest says $WANT" >&2; exit 3
fi
[ "$(sqlite3 "$DB" 'PRAGMA quick_check;' 2>/dev/null | head -1)" = "ok" ] || { echo "lbrain-restore: restored db fails quick_check" >&2; exit 3; }
echo "lbrain-restore: restored $SHAPE home at $H from $B"
echo "  database: $DB (sha256 $GOT matches the backup manifest)"
echo "  now prove it serves: lbrain search '<a phrase you know is in your corpus>'"
exit 0
