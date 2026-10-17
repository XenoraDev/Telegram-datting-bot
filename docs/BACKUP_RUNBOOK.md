# Backup and off-server copy runbook

This is a guide for the person who operates the server. **Nothing here is configured by the
repository**: no cron entry is installed, no remote is set up. Do each step yourself, on the
server, and check it as described.

## What is backed up

`scripts/backup.sh` dumps the whole application database (`pg_dump`, gzip) from the running
`db` container. That is everything the bot knows: users, profiles, matches, payments **and the
payment screenshots**, support tickets, the audit log, plans and tier settings.

It does **not** contain `.env` (bot token, database password, admin session secret). Keep those in
a password manager, separately. A backup without them can be restored; they can be re-issued.

## 1. Run it by hand first

```bash
cd /path/to/dating-bot-v2
./scripts/backup.sh
ls -lh backups/                      # a <db>_<UTC time>.sql.gz of a plausible size
gzip -t backups/<newest file>        # prints nothing when the file is sound
```

`backup.sh` already refuses to keep an empty or invalid dump and deletes this database's backups
older than `BACKUP_KEEP_DAYS` (default 14; set it in `.env`). Files are `chmod 600` in a `700`
directory.

## 2. Schedule it (daily)

Add one line to the crontab of the user that deploys the bot (`crontab -e`), at a quiet hour:

```cron
15 3 * * * cd /path/to/dating-bot-v2 && ./scripts/backup.sh >> backups/backup.log 2>&1
```

aaPanel's *Cron* page can run the same command instead. Next day, confirm it ran: a new file in
`backups/` and no error in `backups/backup.log`.

## 3. Copy it off the server

A backup on the same disk does not survive losing the disk, the account or the server. Pick **one**
of these and run it from cron a little after the backup (for example 03:45):

* **Another machine over SSH.** Create a key used only for this, restrict it on the receiving side
  (a dedicated user, a directory it can only write to), then:
  ```bash
  rsync -a --chmod=F600 backups/ backup-user@other-host:/srv/dating-bot-backups/
  ```
* **An object-storage bucket** with `rclone` (S3, Backblaze B2, Google Drive, ...). Configure the
  remote with `rclone config` (credentials stay in rclone's own config, not in this repository), then:
  ```bash
  rclone copy backups/ remote:dating-bot-backups --include "*.sql.gz"
  ```
* **aaPanel** cloud-storage backup (its own plugin), pointed at the `backups/` directory.

The dumps contain personal data. Before they leave the server, either encrypt them
(`gpg --symmetric` or `age`) or make sure the destination is private, access-controlled and
encrypted at rest. Do not put the key next to the files.

**Retention on the other side:** keep, for example, the last 30 daily files and one per month for
a year. Delete older ones: a deleted account (`/deleteme`) is absent from new backups but stays in
old ones until they age out, so do not keep dumps for ever.

## 4. Know when it stops working

Backups fail silently unless someone looks. One check that exits non-zero when there is no backup
from the last 26 hours (use it in a monitor, or in cron with `MAILTO` set):

```bash
find /path/to/dating-bot-v2/backups -name '*.sql.gz' -mmin -1560 | grep -q .
```

Also watch the free disk space of the server and of the copy destination.

## 5. Practise the restore before you need it

Do this on a **staging** machine or a throw-away copy of the stack, never first on production:

```bash
./scripts/restore.sh backups/<file>.sql.gz --yes    # DESTRUCTIVE: replaces the public schema
```

`restore.sh` stops the bot, replays the dump in one transaction (all or nothing) and starts the
bot again. Afterwards check that `/ping` answers, the admin panel opens, and a known user and a
known payment are there. Note how long it took: that is your recovery time.

## 6. If you ever restore production

1. Say why first (a bad migration, data loss). A normal deploy never needs a restore: migrations are
   additive and a code rollback is `git checkout <previous commit>` and the same `docker compose up`.
2. Take a backup of the current (broken) state anyway; it may still hold something you need.
3. Restore the newest good file with `restore.sh`, then watch the bot log for the first minutes.

## Checklist

| Done | Item |
| --- | --- |
| [ ] | `./scripts/backup.sh` works by hand |
| [ ] | daily cron line installed, first run checked |
| [ ] | off-server copy runs from cron and the copy was seen on the other side |
| [ ] | copies are encrypted or the destination is private |
| [ ] | "no backup in 26 h" check wired to something that tells a person |
| [ ] | a restore was practised on staging |
