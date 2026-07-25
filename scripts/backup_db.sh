#!/bin/bash

# A simple script to perform postgres db backup.

set -o pipefail

DAY=$(date +%A)
TARGET=/home/nelluk/backups/polytopia_bak-${DAY}.sqlc
LOGTARGET=/home/nelluk/backups/polytopia_gamelogs.csv.gz
FULLTARGET=/home/nelluk/polytopia_full_backup.sqlc
IMAGEDIR=/home/nelluk/PolyBot39/data/images
IMAGETARGET=/home/nelluk/backups/polytopia_images-${DAY}.tar.gz

backup_failed=0

# backup db minus log table, and log table minus protected logs, to ~/backups which multiple people have link to
/usr/bin/pg_dump -U nelluk -Fc --exclude-table=gamelog polytopia2 > "$TARGET" || backup_failed=1
/usr/bin/psql -c "COPY (SELECT id, message_ts, guild_id, message FROM gamelog WHERE gamelog.is_protected = FALSE ORDER BY id ASC) TO stdout DELIMITER ',' CSV HEADER" --dbname=polytopia2 | gzip > "$LOGTARGET" || backup_failed=1

# backup full db to one non-rolling location
/usr/bin/pg_dump -U nelluk -Fc polytopia2 > "$FULLTARGET" || backup_failed=1

# backup local images to a weekday-rotated archive
if [ -d "$IMAGEDIR" ]
then
  tar -czf "$IMAGETARGET" -C "$IMAGEDIR" . || backup_failed=1
else
  echo "Image directory $IMAGEDIR does not exist; skipping image backup" >&2
fi

if [ "$backup_failed" -eq 0 ]
then
  echo "Backup successful to file $TARGET"
  exit 0
else
  echo "Error during database or image backup" >&2
  exit 1
fi

# to restore backup:
# drop database dbname;
# pg_restore -C -d postgres backup_file.sqlc -O   (use a built in db name like postgres or nelluk as a place from which to issue the commands)
