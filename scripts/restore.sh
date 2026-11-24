#!/bin/sh
# Restore runbook (product plan v0.0.4, F10.5/v0.0.4 gate).
#
# NEVER restore into the live database without a fresh backup of it first.
# The isolated exercise (docs/deploy.md) restores into a scratch database and
# checks task/history counts before anything touches production.
#
# Usage: TARGET_URL=postgresql://... DATABASE_URL=postgresql://... ./restore.sh <dump-file>
# - TARGET_URL receives the restore (isolated first!).
# - DATABASE_URL is only used for the count comparison against the source.
set -eu

: "${TARGET_URL:?TARGET_URL (restore destination) must be set}"
file="${1:?usage: restore.sh <dump-file>}"
test -f "$file" || { echo "no such dump: $file"; exit 1; }

echo "[$(date -u +%FT%TZ)] restoring $file into TARGET_URL"
createdb "$TARGET_URL" 2>/dev/null || true
pg_restore --clean --if-exists --no-owner --dbname "$TARGET_URL" "$file"

tasks=$(psql "$TARGET_URL" -tAc "SELECT count(*) FROM tasks")
activity=$(psql "$TARGET_URL" -tAc "SELECT count(*) FROM task_activity")
echo "restored: tasks=$tasks activity=$activity"

if [ -n "${DATABASE_URL:-}" ]; then
  src_tasks=$(psql "$DATABASE_URL" -tAc "SELECT count(*) FROM tasks")
  src_activity=$(psql "$DATABASE_URL" -tAc "SELECT count(*) FROM task_activity")
  echo "source:   tasks=$src_tasks activity=$src_activity"
  [ "$tasks" = "$src_tasks" ] || { echo "MISMATCH: task counts differ"; exit 1; }
  [ "$activity" = "$src_activity" ] || { echo "MISMATCH: activity counts differ"; exit 1; }
  echo "counts match the source database."
fi
echo "restore complete."
