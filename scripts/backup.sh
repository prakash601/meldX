#!/bin/sh
# Nightly backup (product plan v0.0.4, owner-approved approach: pg_dump).
#
# Cron example (03:30 IST, keeps 30 daily backups):
#   30 3 * * * /path/to/meldX/scripts/backup.sh >> /var/log/meldx-backup.log 2>&1
#
# VERSION RULE: use pg client tools matching (or older than) your server's
# major version. A newer pg_dump emits settings (e.g. transaction_timeout)
# that an older server rejects during restore.
#
# DATABASE_URL selects the database; BACKUP_DIR stores the dumps. Restore
# with scripts/restore.sh. Rotate OWNER_BOOTSTRAP_SECRET independently —
# backups contain task data only, never credentials or session tokens.
set -eu

: "${DATABASE_URL:?DATABASE_URL must be set (postgresql://...)}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$BACKUP_DIR/meldx-$stamp.dump"

echo "[$(date -u +%FT%TZ)] dumping to $file"
pg_dump --format=custom --no-owner "$DATABASE_URL" > "$file.tmp"
mv "$file.tmp" "$file"

# Retention: drop dumps older than KEEP_DAYS days.
find "$BACKUP_DIR" -name 'meldx-*.dump' -mtime "+$KEEP_DAYS" -delete
echo "[$(date -u +%FT%TZ)] done: $file ($(du -h "$file" | cut -f1))"
