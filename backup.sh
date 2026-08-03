#!/bin/bash
# Backs up the pcflip data folder (SQLite DB + photos) to backup disk.
#
# Two things happen:
#   1. A consistent SQLite backup (via `sqlite3 .backup`, safe to run while
#      the app is live) plus the photos/attachments dirs get rsynced into a
#      dated snapshot folder, hardlinked against the previous snapshot so
#      unchanged files cost ~0 extra disk space.
#   2. Old dated snapshots beyond $RETENTION_DAYS get pruned.
#
# This replaces a plain `rsync --delete` mirror, which had no way to
# recover a previous day's data if the source got corrupted or an item
# was deleted by mistake -- the mirror would just faithfully copy that
# mistake over the only backup you had.

SOURCE_DIR="/home/human1zer/migration/pcflip/data"
DEST_ROOT="/mnt/backup/pcflip"
LOG="/home/human1zer/migration/pcflip/backup.log"
RETENTION_DAYS=30

DATE_STAMP="$(date +%Y-%m-%d_%H%M)"
SNAPSHOT_DIR="$DEST_ROOT/snapshots/$DATE_STAMP"
LATEST_LINK="$DEST_ROOT/latest"
DB_SOURCE="$SOURCE_DIR/pcflip.db"

mkdir -p "$DEST_ROOT/snapshots"

echo "$(date): Starting backup" >> "$LOG"

# Consistent DB snapshot (safe even if the app is writing to the db right now).
if [ -f "$DB_SOURCE" ]; then
    mkdir -p "$SNAPSHOT_DIR"
    /usr/bin/sqlite3 "$DB_SOURCE" ".backup '$SNAPSHOT_DIR/pcflip.db'" >> "$LOG" 2>&1
    DB_RESULT=$?
else
    echo "$(date): pcflip backup FAILED - no db found at $DB_SOURCE" >> "$LOG"
    exit 1
fi

# Photos/attachments: hardlink against the previous snapshot so only
# changed files use new disk space.
if [ -L "$LATEST_LINK" ] || [ -d "$LATEST_LINK" ]; then
    /usr/bin/rsync -a --link-dest="$LATEST_LINK" \
        --exclude pcflip.db \
        "$SOURCE_DIR/" "$SNAPSHOT_DIR/" >> "$LOG" 2>&1
else
    /usr/bin/rsync -a --exclude pcflip.db "$SOURCE_DIR/" "$SNAPSHOT_DIR/" >> "$LOG" 2>&1
fi
RSYNC_RESULT=$?

if [ $DB_RESULT -eq 0 ] && [ $RSYNC_RESULT -eq 0 ]; then
    rm -f "$LATEST_LINK"
    ln -s "$SNAPSHOT_DIR" "$LATEST_LINK"
    echo "$(date): pcflip backup completed successfully -> $SNAPSHOT_DIR" >> "$LOG"

    # Prune snapshots older than RETENTION_DAYS.
    find "$DEST_ROOT/snapshots" -maxdepth 1 -mindepth 1 -type d -mtime "+$RETENTION_DAYS" -exec rm -rf {} \; >> "$LOG" 2>&1
else
    echo "$(date): pcflip backup FAILED (db=$DB_RESULT rsync=$RSYNC_RESULT)" >> "$LOG"
fi
