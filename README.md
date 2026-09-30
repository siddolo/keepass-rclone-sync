# KeePassXC + rclone sync

Synchronizes one `.kdbx` database between Linux computers and storage accessible through rclone, such as Google Drive, Dropbox, OneDrive, S3, WebDAV, or SFTP. It compares content against the last known common version, keeps backups, and stops synchronization when it detects divergent changes.

Requires **Python 3.9+, rclone, and Make**. It uses no pip packages and does not need the database password. Desktop notifications require `notify-send` (on Ubuntu/Debian: `sudo apt install libnotify-bin`).

**About `renameat2`:** this is a Linux system call. The script uses it to atomically exchange the downloaded copy with the local database while preserving the replaced file. When the local database is absent, it uses the same call to create it without overwriting a file that appeared concurrently. This requires support from the kernel, system library (libc), and filesystem. The local database and `data/backup/` must be on the same filesystem. If these requirements are not met, the download stops without replacing the database.

## Getting started

On each computer, as your regular user and without `sudo`:

1. Place the repository in a permanent directory.
2. Put the database at `data/passwords.kdbx` and open that file with KeePassXC. If you use another name, set `database` with `make config`.
3. Run these commands from the repository directory:

   ```bash
   make config
   make auth
   make sync
   make cron-setup
   make check
   ```

In `rclone config`, create a remote named **myremote** (or use the name selected in `remote`), choose the provider, and configure its credentials. The default example is `myremote:keepass/passwords.kdbx`; the remote `keepass/` directory must exist. For S3, include the bucket in the path. If the local and remote files differ during the first synchronization, follow the [conflict resolution procedure](#resolving-a-conflict).

Each computer must have its own `.state/` directory. Do not share it or copy it from another computer. Avoid enabling other jobs that synchronize the same database.

## Commands

| Command | Behavior |
|---|---|
| `make` / `make help` | Lists the available commands |
| `make config` | Creates and edits `config.local.json` as needed, then validates it |
| `make auth` | Opens `rclone config` to choose the provider and configure credentials, using the project's executable and configuration |
| `make sync` | Runs a synchronization immediately, with the same checks and lock used by cron |
| `make cron-setup` | Installs or updates the user's cron job, scheduled every 15 minutes |
| `make check` | Checks configuration, cron, rclone, and access to the remote database |
| `make status` | Shows the last result without contacting the remote |
| `make test` | Runs the local test suite |
| `make i18n-update` | Extracts translatable messages and updates the Italian catalog |
| `make i18n-compile` | Validates and compiles the Italian catalog |

You can also run `make -C /path/to/keepass-rclone-sync <target>`.

### Configuration

`config.json` contains the default values and a generic database and remote example. `config.local.json` contains the real local configuration as overrides and is excluded from Git. It may specify every option or only the values to customize. To choose the editor:

```bash
EDITOR=nano make config
```

The precedence order is `VISUAL`, then `EDITOR`, then the `editor` command. Example local configuration using an rclone remote named `gdrive`, configured for Google Drive:

```json
{
  "database": "passwords.kdbx",
  "remote": "gdrive:keepass/passwords.kdbx",
  "rclone_config": null,
  "retention_days": 90,
  "max_backups": 100
}
```

- `database`: file name inside `data/`, without directories.
- `language`: message language, `en` (default) or `it`.
- `remote`: complete path to the remote file, not its directory.
- `rclone`: executable, default `/usr/bin/rclone`.
- `rclone_config`: `null` uses the user's standard configuration. For a file inside the repository, use `.state/rclone.conf`: `make auth` creates the private directory, which is excluded from Git. Paths outside the repository are also allowed. Relative paths are resolved from the repository; internal paths outside `.state/`, including paths reached through symbolic links, are rejected.
- `settle_seconds`: defers recently saved files; default 2 seconds.
- `command_timeout_seconds`: total timeout for each rclone command; default 180 seconds.

### Language

Messages, command help, logs, and desktop notifications default to English. To use Italian, add this override to `config.local.json`:

```json
{
  "language": "it"
}
```

Override the configured language for a single invocation:

```bash
make status UI_LANGUAGE=en
make help UI_LANGUAGE=it
python3 sync.py --language it --status
python3 manage.py --language it check
```

Precedence is `--language` (or Make's `UI_LANGUAGE`), then configuration, then English. The system locale does not select the language. Cron uses the configured language; an override passed to `make cron-setup` affects setup messages, not the installed job. If the configuration cannot be loaded, startup errors use English unless `--language` is supplied.

Command names, options, configuration keys, and status/action codes remain unchanged. New status records include the English gettext template as `message_id` and its values as `message_args`, alongside the rendered `message`. `make status` renders these records in the selected language without rewriting them. Older records containing only text are displayed as stored. Switching language does not reset the cooldown for structured notifications. Output from external programs such as rclone, and operating-system error details, is displayed as received.

Python's standard-library `gettext` loads the included `locales/it/LC_MESSAGES/keepass-sync.mo` catalog. Missing translations or a missing catalog fall back to English. No translation tools are needed for normal use.

### Providers and authentication

`make auth` starts rclone's interactive configuration without imposing a provider. The name before the colon identifies the configured rclone remote, not the provider type. Examples:

| rclone provider/backend | `remote` example |
|---|---|
| Google Drive | `gdrive:keepass/passwords.kdbx` |
| Dropbox | `dropbox:keepass/passwords.kdbx` |
| OneDrive | `onedrive:keepass/passwords.kdbx` |
| S3 | `s3:my-bucket/keepass/passwords.kdbx` |
| WebDAV | `webdav:keepass/passwords.kdbx` |
| SFTP | `sftp:keepass/passwords.kdbx` |

With SFTP, `sftp:passwords.kdbx` refers to a file in the remote user's home directory, while `sftp:/passwords.kdbx` refers to a file at the filesystem root.

Authentication depends on the provider: browser-based OAuth where supported, credentials or tokens for WebDAV, passwords or SSH keys for SFTP, and credentials or roles for S3. Follow the prompts from `rclone config` and configure read/write access to the selected path. `make check` verifies read access; `make sync` performs any required transfers.

### Cron

`make cron-setup` manages a `BEGIN/END keepass-rclone-sync` block in the user's crontab. It preserves other jobs and replaces a manually added entry for the same script. Running it again with the same parameters leaves the crontab unchanged.

It uses absolute paths for Python and `sync.py` without depending on `pwd`. The script locates the repository from its resolved path, including when invoked through a symbolic link. The last replaced crontab is saved in `.state/crontab.before-setup.txt`.

To change the frequency, provide five numeric cron fields:

```bash
make cron-setup CRON_SCHEDULE='*/30 * * * *'
crontab -l
```

The cron daemon must be running. Run setup as the same user that owns the rclone configuration and desktop session. Run setup again after moving the repository.

### General check

`make check` verifies:

- configuration validity;
- the managed job, current path, absence of duplicates, and an active cron/crond daemon;
- rclone execution;
- the local database and writable directories;
- authenticated remote access, download, and the KDBX header;
- errors or conflicts from the last synchronization result.

It does not modify the crontab, database, backups, or state. The verification download is temporary and is removed. It returns a non-zero status when it finds problems; missing `notify-send` is only a warning. The check does not certify that cron has already run the job after installation.

## Synchronization

Comparison uses the SHA-256 hash of the content, not timestamps or file size.

| Situation | Action |
|---|---|
| Local and remote are identical | Records the common version |
| Only the local file changed | Uploads and verifies by downloading it again |
| Only the remote file changed | Backs up and atomically replaces the local file |
| Both changed and differ | Preserves both copies and reports a conflict |
| First run with two different copies | Reports a conflict |
| First run with only one copy | Creates the missing copy |
| One copy is missing after a successful synchronization | Stops without propagating deletion |
| Network error or duplicate remote files with the same name | Stops |

The remote is checked again before transfer. A lock prevents overlapping runs on the same computer. If the script detects a local save during the operation, it defers synchronization or preserves a conflict.

A download atomically exchanges the local file with the remote copy and preserves the file that was actually replaced. If that file contains a concurrent change, the script reports a conflict that requires `--resolve`, even if the two active files now match. When the local file is absent, creation does not overwrite a file that appeared concurrently.

## Backups

Backups are stored in `data/backup/`:

```text
version-<hostname>-<UTC timestamp>-<sha256>.kdbx
version-elite-20260930T153012123456Z-<64-character hash>.kdbx
```

The hostname identifies the computer that created the backup, including content downloaded from the remote. It is sanitized for use in file names and limited to 64 characters. The timestamp records when the backup was created.

- `version-*` backups save every distinct observed content without duplicating identical copies.
- The local file is backed up before contacting the remote, including during offline runs.
- Retention is **90 days and at most 100 versions**, while always keeping the newest version.
- Retention manages only files matching the documented format directly under `data/backup/`, ordered by timestamp. Files with other names are not managed.
- Copies under `data/backup/conflicts/<local-hash>-<remote-hash>/` are named `local-<hostname>.kdbx` and `remote.kdbx`. A missing file is represented as `missing`. These copies do not expire; remove them after resolving the conflict.
- `data/backup/replaced/<timestamp>-<id>/local-<hostname>.kdbx` preserves the file actually replaced during each download, including later writes from an editor that already had it open. These files do not expire. Remove them only after closing the database in the editor and verifying the result. If the process stops before the exchange, the prepared remote copy may remain there instead.

Backups and logs are local to each computer. `data/`, `.state/`, and `config.local.json` are excluded from Git; placeholder files keep the data directories in the repository.

To restore a backup, close the database in KeePassXC, copy the selected version over the file in `data/`, reopen it, and verify its entries. The restored file is treated as a local change.

## Resolving a conflict

The notification and `.state/conflict.json` identify the preserved copies.

1. Avoid changes from other computers while resolving the conflict.
2. Open the database from `data/` in KeePassXC and select **Database -> Merge From Database**, choosing the conflict's `remote.kdbx`. If the message reports a concurrent local save during download, the active file already contains the remote version; merge the original from the indicated `backup/replaced/` path instead.
3. Save, verify the entries and history, then close the database.
4. Publish the result:

   ```bash
   ./sync.py --resolve local
   ```

To select the remote copy in full, run `./sync.py --resolve remote`. Both commands preserve the current copies before replacing anything and can recreate a missing file. Do not put them in cron. A successful run clears the active conflict, but the protected copies remain.

## Notifications and logs

To test notifications in the desktop session:

```bash
./sync.py --test-notification
```

The script discovers the user's D-Bus even when run from cron; `DISPLAY` does not need to be set. Conflicts and errors produce critical notifications, while downloads produce informational notifications. The same message is sent at most once per hour.

The relevant settings are `notifications`, `notify_downloads`, and `notification_cooldown_seconds`. Display behavior depends on the desktop environment and its settings. If delivery fails, the problem remains in the logs; notifications are not queued for the next login.

Logs are written to stderr and `.state/sync.log`, with rotation up to approximately 4 MiB. The last result is stored in `.state/status.json`. If writing the status fails, the script still attempts to send an error notification and the file may contain an older result. The script returns `0` for success or deferral, `1` for an error, and `2` for a conflict; Make reports a failure when the command returns non-zero.

## Using KeePassXC

Verify how KeePassXC reloads external changes and handles merging when it has unsaved changes. When switching computers, save and close the database and run `make sync` on the first computer; run it on the second computer before opening the database.

The script sees only files on disk, not unsaved in-memory changes. A save from a stale editor can appear as a normal local change. Rechecks reduce races but do not provide a distributed lock; a window remains between checking and writing. The script verifies the header and hash, not cryptographic validity or entry completeness, so open and verify the database with KeePassXC.

## Tests

`make test` uses synthetic files and temporary directories. It verifies synchronization, concurrent saves, conflicts, retention, notifications, paths containing shell characters, and cron management. When Git is installed, it verifies exclusion of internal credentials; when rclone is installed, it also tests transfers against the local backend. It does not use your remotes or database.

### Maintaining translations

Translation maintenance requires GNU gettext (`sudo apt install gettext` on Ubuntu/Debian):

```bash
make i18n-update
# Edit locales/it/LC_MESSAGES/keepass-sync.po.
make i18n-compile
make test
```

- Keep English source templates inside `_()` and insert named values after translation, for example `_("Corrupted backup: {path}").format(path=str(path))`.
- Do not pass f-strings to `_()`.
- Preserve placeholders such as `{path}` in translations.
- Include the `.pot`, `.po`, and compiled `.mo` files when distributing changes.

## License

This project is available under the [MIT License](LICENSE).
