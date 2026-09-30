#!/usr/bin/env python3
"""Conservative synchronization of one KDBX file using Python stdlib and rclone."""

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import i18n
from i18n import _


ROOT = Path(__file__).resolve().parent
KDBX_SIGNATURE = bytes.fromhex("03d9a29a67fb4bb5")
LOG = logging.getLogger("keepass-rclone-sync")
BACKUP_NAME = re.compile(
    r"version-[A-Za-z0-9][A-Za-z0-9._-]{0,63}-"
    r"(?P<timestamp>\d{8}T\d{12}Z)-[0-9a-f]{64}\.kdbx"
)


class SyncError(Exception):
    pass


class Conflict(SyncError):
    pass


class RetryLater(SyncError):
    pass


def host_label():
    """Return the hostname sanitized for use as a filename component."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("._-")[:64] or "unknown-host"


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_kdbx(path):
    with path.open("rb") as source:
        header = source.read(12)
    if len(header) != 12 or header[:8] != KDBX_SIGNATURE:
        raise SyncError(_("Empty file or invalid KDBX header: {name}").format(name=path.name))


def atomic_copy(source, destination):
    """Write to a temporary file on the same filesystem, then rename atomically."""
    fd, name = tempfile.mkstemp(prefix=".write-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def rename_checked(source, destination, *, exchange):
    """On Linux, exchange two names or create destination without overwriting it."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise SyncError(_("libc does not support renameat2: local download stopped")) from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    # AT_FDCWD; RENAME_EXCHANGE / RENAME_NOREPLACE. Never use a destructive fallback.
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 2 if exchange else 1):
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV):
            raise SyncError(_("Download stopped: renameat2 is required and data/backup must be "
                              "on the same filesystem as the database"))
        raise OSError(error, os.strerror(error), str(source), None, str(destination))


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise SyncError(_("Cannot read {path}: {error}").format(path=str(path), error=str(exc))) from exc


def load_config(root):
    config = read_json(root / "config.json")
    if not isinstance(config, dict):
        raise SyncError(_("config.json is missing or invalid"))
    config.setdefault("language", "en")
    local = read_json(root / "config.local.json")
    if local is not None:
        if not isinstance(local, dict) or set(local) - set(config):
            raise SyncError(_("Invalid keys in config.local.json"))
        config.update(local)
    config.setdefault("language", "en")
    if config["language"] not in i18n.LANGUAGES:
        raise SyncError(_("language must be en or it"))
    name = config["database"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".kdbx"):
        raise SyncError(_("database must be a .kdbx file name without directories"))
    if not isinstance(config["remote"], str) or not config["remote"].endswith(".kdbx"):
        raise SyncError(_("remote must be the complete path to the .kdbx file"))
    for key in ("retention_days", "max_backups", "command_timeout_seconds"):
        if type(config[key]) is not int or config[key] < 1:
            raise SyncError(_("{key} must be a positive integer").format(key=key))
    for key in ("settle_seconds", "notification_cooldown_seconds"):
        if type(config[key]) is not int or config[key] < 0:
            raise SyncError(_("{key} must be a non-negative integer").format(key=key))
    for key in ("notifications", "notify_downloads"):
        if type(config[key]) is not bool:
            raise SyncError(_("{key} must be true or false").format(key=key))
    if not isinstance(config["rclone"], str) or not config["rclone"]:
        raise SyncError(_("rclone must be an executable name or an absolute path"))
    if config["rclone_config"] is not None:
        if not isinstance(config["rclone_config"], str) or not config["rclone_config"]:
            raise SyncError(_("rclone_config must be null or a path"))
        credentials = (root / config["rclone_config"]).resolve()
        repository = root.resolve()
        if (credentials.is_relative_to(repository)
                and not credentials.parent.is_relative_to(repository / ".state")):
            raise SyncError(_("Store rclone credentials in Git-excluded .state/ "
                              "(e.g. .state/rclone.conf) or outside the repository"))
        config["rclone_config"] = str(credentials)
    return config


@contextmanager
def execution_lock(state_dir):
    with (state_dir / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RetryLater(_("Another local synchronization is already running"))
        yield


class Rclone:
    def __init__(self, config):
        self.config = config
        self.remote = config["remote"]

    def run(self, *arguments):
        command = [self.config["rclone"], *map(str, arguments),
                   "--retries", "1", "--contimeout", "15s", "--timeout", "30s"]
        if self.config["rclone_config"]:
            command += ["--config", self.config["rclone_config"]]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=self.config["command_timeout_seconds"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(_("rclone unavailable or timed out: {error}").format(error=str(exc))) from exc
        if result.returncode:
            raise SyncError(_("rclone {command} failed ({code}): {error}").format(
                command=arguments[0], code=result.returncode, error=result.stderr.strip()[-2000:]))
        return result.stdout

    def fetch(self, destination):
        # A successful listing distinguishes a missing file from network errors.
        # Some providers allow multiple files with the same name.
        if "/" in self.remote:
            parent, name = self.remote.rsplit("/", 1)
            # Preserve the root: sftp:/file != sftp:file, /file != file.
            parent += "/"
        elif ":" in self.remote:
            parent, name = self.remote.rsplit(":", 1)
            parent += ":"
        else:
            parent, name = ".", self.remote
        listing = json.loads(self.run("lsjson", parent, "--max-depth", "1"))
        matches = [item for item in listing if item["Name"] == name]
        if len(matches) > 1:
            raise SyncError(_("Multiple remote files have the same name: resolve remote duplicates"))
        if not matches:
            return None
        if matches[0].get("IsDir"):
            raise SyncError(_("The remote path is a directory, not a database"))
        self.run("copyto", self.remote, destination, "--ignore-times")
        validate_kdbx(destination)
        return digest(destination)

    def upload(self, source):
        self.run("copyto", source, self.remote, "--ignore-times")


class Synchronizer:
    def __init__(self, root, config, transport=None):
        self.root = root
        self.config = config
        self.hostname = host_label()
        self.data = root / "data"
        self.backups = self.data / "backup"
        self.state_dir = root / ".state"
        self.local = self.data / config["database"]
        self.transport = transport or Rclone(config)
        self.identity = {"database": config["database"], "remote": config["remote"]}
        for directory in (self.data, self.backups, self.state_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def snapshot_local(self, destination):
        if not self.local.exists():
            return None
        before = self.local.stat()
        if time.time() - before.st_mtime < self.config["settle_seconds"]:
            raise RetryLater(_("Database recently saved: retrying on the next run"))
        atomic_copy(self.local, destination)
        after = self.local.stat()
        fields = ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise RetryLater(_("Database changed while being read"))
        validate_kdbx(destination)
        value = digest(destination)
        self.assert_local(value)
        return value

    def assert_local(self, expected):
        current = digest(self.local) if self.local.exists() else None
        if current != expected:
            raise RetryLater(_("Local database changed during synchronization"))

    def replace_local(self, remote_copy, local_hash, remote_hash):
        # The exchange preserves the inode actually replaced, not just the earlier
        # snapshot. Do not delete it: an editor may still have that file open.
        replaced = self.backups / "replaced"
        replaced.mkdir(mode=0o700, exist_ok=True)
        fsync_directory(self.backups)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = replaced / f"{stamp}-{uuid.uuid4().hex}"
        directory.mkdir(mode=0o700)
        fsync_directory(replaced)
        saved = directory / f"local-{self.hostname}.kdbx"
        try:
            atomic_copy(remote_copy, saved)
            self.assert_local(local_hash)
            rename_checked(saved, self.local, exchange=local_hash is not None)
        except Exception as exc:
            # Before the exchange, saved only contains the download. After the
            # exchange, no error handler may delete the displaced local file.
            saved.unlink(missing_ok=True)
            directory.rmdir()
            if isinstance(exc, (FileExistsError, FileNotFoundError)):
                raise RetryLater(_("Local database created or removed during download")) from exc
            raise
        fsync_directory(directory)
        fsync_directory(self.data)
        if local_hash is None:
            directory.rmdir()
            fsync_directory(replaced)
        else:
            saved_hash = digest(saved)
            if saved_hash != local_hash:
                raise Conflict(self.preserve_conflict(
                    saved, saved_hash, remote_copy, remote_hash,
                    _("Concurrent local save: original preserved at {path}").format(path=str(saved)),
                    requires_resolution=True))

    def backup(self, source, value):
        existing = [p for p in self.backups.glob(f"version-*-{value}.kdbx")
                    if p.is_file() and BACKUP_NAME.fullmatch(p.name)]
        if existing:
            if digest(existing[0]) != value:
                raise SyncError(_("Corrupted backup: {path}").format(path=str(existing[0])))
            return existing[0]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backups / f"version-{self.hostname}-{stamp}-{value}.kdbx"
        atomic_copy(source, destination)
        return destination

    def prune(self):
        # Only manage files created by this script, never conflicts or manual files.
        versions = []
        for path in self.backups.iterdir():
            match = BACKUP_NAME.fullmatch(path.name)
            if path.is_file() and match:
                versions.append((match["timestamp"], path))
        versions.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        cutoff = time.time() - self.config["retention_days"] * 86400
        for index, (_, path) in enumerate(versions):
            # Keep at least the newest version, even when it is very old.
            if index and (index >= self.config["max_backups"] or path.stat().st_mtime < cutoff):
                path.unlink()

    def preserve_conflict(self, local_copy, local_hash, remote_copy, remote_hash, reason,
                          requires_resolution=False):
        pair = f"{local_hash or 'missing'}-{remote_hash or 'missing'}"
        directory = self.backups / "conflicts" / pair
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        copies = {}
        for side, source, value in (("local", local_copy, local_hash),
                                    ("remote", remote_copy, remote_hash)):
            if value:
                filename = f"local-{self.hostname}.kdbx" if side == "local" else "remote.kdbx"
                destination = directory / filename
                if not destination.exists():
                    atomic_copy(source, destination)
                elif digest(destination) != value:
                    raise SyncError(_("Corrupted conflict copy: {path}").format(path=str(destination)))
                copies[side] = str(destination.relative_to(self.root))
        write_json(self.state_dir / "conflict.json", {
            "reason": reason, "local_sha256": local_hash, "remote_sha256": remote_hash,
            "reason_message": i18n.message_fields(reason),
            "copies": copies, "detected_at": datetime.now(timezone.utc).isoformat(),
            "requires_resolution": requires_resolution,
        })
        return _("{reason}. Copies preserved in {directory}").format(reason=reason, directory=str(directory))

    def synchronize(self, resolve=None):
        conflict = read_json(self.state_dir / "conflict.json")
        if isinstance(conflict, dict) and conflict.get("requires_resolution") and not resolve:
            reason = i18n.render(conflict.get("reason_message", {}), conflict.get("reason", ""))
            raise Conflict(_("{reason}. Merge the preserved copy and use --resolve").format(reason=reason))
        state = read_json(self.state_dir / "baseline.json")
        if state is not None:
            if (not isinstance(state, dict) or state.get("identity") != self.identity
                    or not re.fullmatch(r"[0-9a-f]{64}", str(state.get("sha256", "")))):
                raise SyncError(_("Incompatible or corrupted state: check configuration and baseline.json"))
        base = state["sha256"] if state else None
        with tempfile.TemporaryDirectory(prefix="run-", dir=self.state_dir) as working:
            working = Path(working)
            local_copy, remote_copy = working / "local.kdbx", working / "remote.kdbx"
            local_hash = self.snapshot_local(local_copy)
            if local_hash:
                # Back up the local file before any remote access, including offline runs.
                self.backup(local_copy, local_hash)
                self.prune()
            remote_hash = self.transport.fetch(remote_copy)
            if remote_hash:
                self.backup(remote_copy, remote_hash)
                self.prune()
            self.assert_local(local_hash)

            if local_hash == remote_hash:
                if local_hash is None:
                    raise SyncError(_("Database missing from both data/ and the remote"))
                action = "unchanged"
            elif resolve:
                selected = local_hash if resolve == "local" else remote_hash
                if selected is None:
                    raise SyncError(_("Cannot select {side}: the file is missing").format(side=resolve))
                self.preserve_conflict(local_copy, local_hash, remote_copy, remote_hash,
                                       _("Copies before manual resolution"))
                action = "upload" if resolve == "local" else "download"
            elif state and (local_hash is None or remote_hash is None):
                reason = _("A previously synchronized copy was removed")
                raise Conflict(self.preserve_conflict(local_copy, local_hash,
                                                     remote_copy, remote_hash, reason))
            elif base is None:
                if local_hash is None:
                    action = "download"
                elif remote_hash is None:
                    action = "upload"
                else:
                    raise Conflict(self.preserve_conflict(
                        local_copy, local_hash, remote_copy, remote_hash,
                        _("First run: different copies, no known common version")))
            elif remote_hash == base:
                action = "upload"
            elif local_hash == base:
                action = "download"
            else:
                raise Conflict(self.preserve_conflict(
                    local_copy, local_hash, remote_copy, remote_hash,
                    _("Both copies have changed since the last synchronization")))

            if action != "unchanged":
                # Recheck the remote before changing either active file.
                check_copy = working / "check.kdbx"
                check_hash = self.transport.fetch(check_copy)
                if check_hash != remote_hash:
                    raise Conflict(self.preserve_conflict(
                        local_copy, local_hash, check_copy, check_hash,
                        _("The remote changed during synchronization")))
                self.assert_local(local_hash)
                if action == "upload":
                    self.transport.upload(local_copy)
                    result = local_hash
                    verify_copy = working / "verify.kdbx"
                    verify_hash = self.transport.fetch(verify_copy)
                    if verify_hash != result:
                        raise Conflict(self.preserve_conflict(
                            local_copy, local_hash, verify_copy, verify_hash,
                            _("Upload verification failed or concurrent update detected")))
                    self.assert_local(local_hash)
                else:
                    self.replace_local(remote_copy, local_hash, remote_hash)
                    result = remote_hash
                    self.assert_local(result)
            else:
                result = local_hash
                self.assert_local(result)

            write_json(self.state_dir / "baseline.json", {
                "identity": self.identity, "sha256": result,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            })
            (self.state_dir / "conflict.json").unlink(missing_ok=True)
            self.prune()
            return action


def notify(config, state_dir, message, critical=True, force=False):
    if not config["notifications"] and not force:
        return False
    notifier = shutil.which("notify-send")
    if notifier is None:
        LOG.warning(_("Desktop notification not sent: install notify-send (libnotify-bin package)"))
        return False
    notice_path = state_dir / "notification.json"
    try:
        previous = read_json(notice_path) or {}
        if not isinstance(previous, dict) or not isinstance(previous.get("time", 0), (int, float)):
            raise SyncError(_("Invalid notification state format"))
    except SyncError as exc:
        LOG.warning(_("Ignoring invalid notification state: {error}").format(error=str(exc)))
        previous = {}
    key = hashlib.sha256(json.dumps(i18n.message_fields(message), sort_keys=True).encode()).hexdigest()
    if (not force and previous.get("key") == key
            and time.time() - previous.get("time", 0) < config["notification_cooldown_seconds"]):
        return True
    env = os.environ.copy()
    runtime = Path(f"/run/user/{os.getuid()}")
    if (runtime / "bus").exists():
        env.setdefault("XDG_RUNTIME_DIR", str(runtime))
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    try:
        result = subprocess.run(
            [notifier, "--app-name=KeePass rclone sync", "--urgency",
             "critical" if critical else "normal", "--expire-time", "0" if critical else "10000",
             "--", _("KeePass: synchronization"), message],
            env=env, capture_output=True, text=True, timeout=8,
        )
        if result.returncode:
            LOG.warning(_("Desktop notification not sent: {error}").format(error=result.stderr.strip()))
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning(_("Desktop notification not sent: {error}").format(error=str(exc)))
        return False
    try:
        write_json(notice_path, {"key": key, "time": time.time()})
    except OSError as exc:
        LOG.warning(_("Notification sent, but cooldown could not be saved: {error}").format(error=str(exc)))
    return True


def setup_logging(state_dir):
    LOG.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logfile = RotatingFileHandler(state_dir / "sync.log", maxBytes=1024 * 1024,
                                 backupCount=3)
    logfile.setFormatter(formatter)
    LOG.addHandler(console)
    LOG.addHandler(logfile)


def report_error(config, state_dir, exc):
    message = exc.args[0] if exc.args and isinstance(exc.args[0], i18n.Message) else str(exc)
    LOG.error(message)
    try:
        write_json(state_dir / "status.json", {
            "status": "conflict" if isinstance(exc, Conflict) else "error",
            "message": message, "time": datetime.now(timezone.utc).isoformat(),
            **i18n.message_fields(message),
        })
    except OSError as status_error:
        LOG.warning(_("Cannot save error status: {error}").format(error=str(status_error)))
    try:
        notify(config, state_dir, message)
    except (SyncError, OSError, ValueError, TypeError, AttributeError) as notification_error:
        LOG.warning(_("Notification state error: {error}").format(error=str(notification_error)))
    return 2 if isinstance(exc, Conflict) else 1


def run_job(root, config, args):
    """Run with the lock held and handle operational errors."""
    state_dir = root / ".state"
    try:
        if args.status:
            status = read_json(state_dir / "status.json") or {"status": "never_run"}
            if "message" in status or "message_id" in status:
                status["message"] = i18n.render(status)
            print(json.dumps(status, indent=2, ensure_ascii=False))
            return 0
        if args.test_notification:
            return 0 if notify(config, state_dir, _("Test: desktop notifications are working"),
                               critical=False, force=True) else 1
        action = Synchronizer(root, config).synchronize(args.resolve)
        message = {"upload": _("Database uploaded and verified"),
                   "download": _("Local database updated from the remote"),
                   "unchanged": _("Databases are already synchronized")}[action]
        write_json(state_dir / "status.json", {
            "status": "ok", "action": action, "message": message,
            **i18n.message_fields(message),
            "time": datetime.now(timezone.utc).isoformat(),
        })
        LOG.info(message)
        if action == "download" and config["notify_downloads"]:
            notify(config, state_dir, _("{message}. Check that KeePassXC reloads the database.").format(message=message),
                   critical=False)
        return 0
    except RetryLater as exc:
        message = exc.args[0] if exc.args and isinstance(exc.args[0], i18n.Message) else str(exc)
        LOG.info(_("Deferred: {reason}").format(reason=message))
        try:
            write_json(state_dir / "status.json", {
                "status": "deferred", "message": str(exc),
                **i18n.message_fields(message),
                "time": datetime.now(timezone.utc).isoformat(),
            })
        except OSError as status_error:
            return report_error(config, state_dir, status_error)
        return 0
    except (SyncError, OSError, ValueError, KeyError, TypeError) as exc:
        return report_error(config, state_dir, exc)


def main():
    language = i18n.select_cli_language(sys.argv[1:])
    try:
        config = load_config(ROOT)
    except (SyncError, OSError, ValueError, KeyError, TypeError):
        config = None
    if config and not language:
        i18n.set_language(config["language"])
    i18n.configure_argparse()
    parser = argparse.ArgumentParser(allow_abbrev=False, description=_("Conservative synchronization of one KDBX file using Python stdlib and rclone."))
    i18n.add_language_option(parser)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--resolve", choices=("local", "remote"),
                         help=_("Explicitly select a copy; preserve both before replacing"))
    options.add_argument("--test-notification", action="store_true",
                         help=_("Test desktop notifications without accessing databases"))
    options.add_argument("--status", action="store_true", help=_("Show the last result without network access"))
    args = parser.parse_args()
    os.umask(0o077)
    state_dir = ROOT / ".state"
    try:
        state_dir.mkdir(mode=0o700, exist_ok=True)
        setup_logging(state_dir)
        with execution_lock(state_dir):
            config = load_config(ROOT)
            i18n.set_language(args.language or config["language"])
            return run_job(ROOT, config, args)
    except RetryLater as exc:
        LOG.info(_("Deferred: {reason}").format(reason=str(exc)))
        return 0
    except (SyncError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error(_("Startup error: {error}").format(error=str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
