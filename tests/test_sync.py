import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("keepass_sync", ROOT / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
DEFAULTS = json.loads((ROOT / "config.json").read_text())


def database(label):
    # Transport fixture, not a real encrypted and decryptable database.
    return sync.KDBX_SIGNATURE + b"\x00\x00\x04\x00" + label.encode()


class Remote:
    def __init__(self, content=None):
        self.content = content
        self.fetches = 0
        self.uploads = 0
        self.on_fetch = None
        self.on_upload = None

    def fetch(self, destination):
        self.fetches += 1
        if self.on_fetch:
            self.on_fetch(self)
        if self.content is None:
            return None
        destination.write_bytes(self.content)
        sync.validate_kdbx(destination)
        return sync.digest(destination)

    def upload(self, source):
        self.uploads += 1
        if self.on_upload:
            self.on_upload(self)
        else:
            self.content = source.read_bytes()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(DEFAULTS)
        self.config.update(settle_seconds=0, notifications=False)
        self.remote = Remote(database("base"))
        with patch.object(sync.socket, "gethostname", return_value="elite"):
            self.job = sync.Synchronizer(self.root, self.config, self.remote)
        self.job.local.write_bytes(self.remote.content)

    def establish(self):
        self.assertEqual(self.job.synchronize(), "unchanged")

    def baseline(self):
        return (self.job.state_dir / "baseline.json").read_bytes()

    def versions(self):
        return list(self.job.backups.glob("version-*.kdbx"))

    def test_same_content_different_timestamps_no_transfer_or_duplicate_backup(self):
        self.establish()
        os.utime(self.job.local, (1, 1))
        self.assertEqual(self.job.synchronize(), "unchanged")
        self.assertEqual(self.remote.uploads, 0)
        self.assertEqual(len(self.versions()), 1)

    def test_new_backup_and_conflict_names_include_local_hostname(self):
        self.establish()
        self.assertTrue(self.versions()[0].name.startswith("version-elite-"))
        self.assertIsNotNone(sync.BACKUP_NAME.fullmatch(self.versions()[0].name))
        self.job.local.write_bytes(database("local-edit"))
        self.remote.content = database("remote-edit")
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        conflict = sync.read_json(self.job.state_dir / "conflict.json")
        self.assertEqual(Path(conflict["copies"]["local"]).name, "local-elite.kdbx")
        self.assertEqual(Path(conflict["copies"]["remote"]).name, "remote.kdbx")

    def test_unrecognized_backup_names_are_not_reused_or_pruned(self):
        value = sync.digest(self.job.local)
        unmanaged = self.job.backups / f"version-20200101T120000000000Z-{value}.kdbx"
        unmanaged.write_bytes(self.job.local.read_bytes())
        os.utime(unmanaged, (1, 1))
        current = self.job.backup(self.job.local, value)
        self.assertNotEqual(current, unmanaged)
        self.assertTrue(current.name.startswith("version-elite-"))
        self.assertIsNone(sync.BACKUP_NAME.fullmatch(unmanaged.name))
        self.job.prune()
        self.assertTrue(unmanaged.exists())

    def test_retention_orders_mixed_host_names_by_timestamp(self):
        self.config["max_backups"] = 2
        versions = []
        for prefix, stamp in (("zeta-", "20200101T120000000000Z"),
                              ("elite-", "20210101T120000000000Z"),
                              ("alpha-", "20220101T120000000000Z")):
            source = self.root / "fixture.kdbx"
            source.write_bytes(database(stamp))
            path = self.job.backups / f"version-{prefix}{stamp}-{sync.digest(source)}.kdbx"
            path.write_bytes(source.read_bytes())
            versions.append(path)
        self.job.prune()
        self.assertFalse(versions[0].exists())
        self.assertTrue(versions[1].exists())
        self.assertTrue(versions[2].exists())

    def test_local_change_uploaded_regardless_of_timestamp(self):
        self.establish()
        self.job.local.write_bytes(database("local"))
        os.utime(self.job.local, (1, 1))
        self.assertEqual(self.job.synchronize(), "upload")
        self.assertEqual(self.remote.content, database("local"))
        self.assertEqual({p.read_bytes() for p in self.versions()},
                         {database("base"), database("local")})

    def test_remote_change_downloaded_with_backup(self):
        self.establish()
        self.remote.content = database("remote")
        self.assertEqual(self.job.synchronize(), "download")
        self.assertEqual(self.job.local.read_bytes(), database("remote"))
        self.assertIn(database("base"), {p.read_bytes() for p in self.versions()})

    def test_two_laptops_divergence_retains_both_and_baseline(self):
        second = sync.Synchronizer(self.root / "laptop-b", self.config, self.remote)
        second.local.write_bytes(database("base"))
        self.establish()
        second.synchronize()
        before = self.baseline()
        self.job.local.write_bytes(database("credential-X"))
        second.local.write_bytes(database("credential-Y"))
        self.assertEqual(second.synchronize(), "upload")
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertEqual(self.job.local.read_bytes(), database("credential-X"))
        self.assertEqual(self.remote.content, database("credential-Y"))
        self.assertEqual(self.baseline(), before)
        conflict = sync.read_json(self.job.state_dir / "conflict.json")
        self.assertEqual({(self.root / p).read_bytes() for p in conflict["copies"].values()},
                         {database("credential-X"), database("credential-Y")})
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertEqual(len(list((self.job.backups / "conflicts").glob("*/*.kdbx"))), 2)

    def test_first_run_different_copies_does_not_choose_newest(self):
        self.job.local.write_bytes(database("different"))
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertFalse((self.job.state_dir / "baseline.json").exists())
        self.assertEqual(self.remote.uploads, 0)

    def test_first_run_one_copy_bootstraps_other(self):
        self.job.local.unlink()
        self.assertEqual(self.job.synchronize(), "download")
        self.assertEqual(self.job.local.read_bytes(), database("base"))
        (self.job.state_dir / "baseline.json").unlink()
        self.remote.content = None
        self.assertEqual(self.job.synchronize(), "upload")
        self.assertEqual(self.remote.content, database("base"))

    def test_known_missing_remote_requires_explicit_resolution(self):
        self.establish()
        self.remote.content = None
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertIsNone(self.remote.content)
        self.assertEqual(self.job.synchronize("local"), "upload")

    def test_known_missing_local_requires_explicit_resolution(self):
        self.establish()
        self.job.local.unlink()
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertFalse(self.job.local.exists())
        self.assertEqual(self.job.synchronize("remote"), "download")

    def test_both_absent_is_error(self):
        self.job.local.unlink()
        self.remote.content = None
        with self.assertRaises(sync.SyncError):
            self.job.synchronize()

    def test_resolve_local_after_manual_merge_preserves_preimages(self):
        self.establish()
        self.job.local.write_bytes(database("X"))
        self.remote.content = database("Y")
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.job.local.write_bytes(database("merged-X-Y"))
        self.assertEqual(self.job.synchronize("local"), "upload")
        self.assertEqual(self.remote.content, database("merged-X-Y"))
        self.assertFalse((self.job.state_dir / "conflict.json").exists())
        copies = {p.read_bytes() for p in (self.job.backups / "conflicts").glob("*/*.kdbx")}
        self.assertEqual(copies, {database("X"), database("Y"), database("merged-X-Y")})

    def test_resolve_remote_keeps_discarded_local(self):
        self.establish()
        self.job.local.write_bytes(database("X"))
        self.remote.content = database("Y")
        self.assertEqual(self.job.synchronize("remote"), "download")
        self.assertEqual(self.job.local.read_bytes(), database("Y"))
        copies = {p.read_bytes() for p in (self.job.backups / "conflicts").glob("*/*.kdbx")}
        self.assertEqual(copies, {database("X"), database("Y")})

    def test_read_failure_still_backs_up_local_and_keeps_baseline(self):
        self.establish()
        before = self.baseline()
        self.job.local.write_bytes(database("offline-edit"))
        with patch.object(self.remote, "fetch", side_effect=sync.SyncError("offline")):
            with self.assertRaises(sync.SyncError):
                self.job.synchronize()
        self.assertEqual(self.baseline(), before)
        self.assertIn(database("offline-edit"), {p.read_bytes() for p in self.versions()})

    def test_backup_failure_prevents_any_remote_access(self):
        with patch.object(self.job, "backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.job.synchronize()
        self.assertEqual(self.remote.fetches, 0)

    def test_upload_failure_does_not_commit_baseline(self):
        self.establish()
        before = self.baseline()
        self.job.local.write_bytes(database("new"))
        with patch.object(self.remote, "upload", side_effect=sync.SyncError("offline")):
            with self.assertRaises(sync.SyncError):
                self.job.synchronize()
        self.assertEqual(self.baseline(), before)
        self.assertEqual(self.remote.content, database("base"))

    def test_upload_verification_failure_protects_local_and_remote(self):
        self.establish()
        before = self.baseline()
        self.job.local.write_bytes(database("new"))
        self.remote.on_upload = lambda remote: setattr(remote, "content", database("other"))
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertEqual(self.baseline(), before)
        self.assertEqual(self.job.local.read_bytes(), database("new"))

    def test_run_job_records_deferred_without_touching_baseline(self):
        self.establish()
        before = self.baseline()
        self.config["settle_seconds"] = 10
        self.job.local.write_bytes(database("just-saved"))
        args = argparse.Namespace(status=False, test_notification=False, resolve=None)
        with patch.object(sync, "Synchronizer", return_value=self.job):
            self.assertEqual(sync.run_job(self.root, self.config, args), 0)
        self.assertEqual(sync.read_json(self.job.state_dir / "status.json")["status"], "deferred")
        self.assertEqual(self.baseline(), before)

    def test_status_write_failure_does_not_suppress_error_or_conflict_notification(self):
        args = argparse.Namespace(status=False, test_notification=False, resolve=None)
        for error, code in ((OSError("original IO error"), 1),
                            (sync.Conflict("original conflict"), 2)):
            with self.subTest(error=error), \
                    patch.object(sync, "Synchronizer", return_value=self.job), \
                    patch.object(self.job, "synchronize", side_effect=error), \
                    patch.object(sync, "write_json", side_effect=OSError("disk full")), \
                    patch.object(sync, "notify", return_value=True) as notify, \
                    self.assertLogs(sync.LOG, level="WARNING"):
                self.assertEqual(sync.run_job(self.root, self.config, args), code)
                notify.assert_called_once_with(self.config, self.job.state_dir, str(error))

    def test_deferred_status_write_failure_is_not_reported_as_success(self):
        args = argparse.Namespace(status=False, test_notification=False, resolve=None)
        with patch.object(sync, "Synchronizer", return_value=self.job), \
                patch.object(self.job, "synchronize", side_effect=sync.RetryLater("busy")), \
                patch.object(sync, "write_json", side_effect=OSError("disk full")), \
                patch.object(sync, "notify", return_value=True) as notify, \
                self.assertLogs(sync.LOG, level="WARNING"):
            self.assertEqual(sync.run_job(self.root, self.config, args), 1)
        notify.assert_called_once_with(self.config, self.job.state_dir, "disk full")

    def test_remote_changes_between_checks_no_upload(self):
        self.establish()
        self.job.local.write_bytes(database("new"))
        self.remote.fetches = 0

        def mutate(remote):
            if remote.fetches == 2:
                remote.content = database("other-laptop")

        self.remote.on_fetch = mutate
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertEqual(self.remote.uploads, 0)

    def test_local_changes_during_download_prevent_replacement(self):
        self.establish()
        self.remote.content = database("remote-new")
        self.remote.on_fetch = lambda remote: self.job.local.write_bytes(database("editing"))
        with self.assertRaises(sync.RetryLater):
            self.job.synchronize()
        self.assertEqual(self.job.local.read_bytes(), database("editing"))

    def test_save_at_exchange_is_preserved_and_requires_resolution(self):
        self.establish()
        before = self.baseline()
        self.remote.content = database("remote-new")
        rename = sync.rename_checked

        def save_then_exchange(source, destination, **kwargs):
            incoming = self.job.data / "editor-save.kdbx"
            incoming.write_bytes(database("concurrent-save"))
            os.replace(incoming, self.job.local)
            rename(source, destination, **kwargs)

        with patch.object(sync, "rename_checked", side_effect=save_then_exchange):
            with self.assertRaises(sync.Conflict):
                self.job.synchronize()
        self.assertEqual(self.baseline(), before)
        conflict = sync.read_json(self.job.state_dir / "conflict.json")
        self.assertEqual((self.root / conflict["copies"]["local"]).read_bytes(),
                         database("concurrent-save"))
        self.assertEqual(self.job.local.read_bytes(), database("remote-new"))
        # The active files now match, but that does not resolve the displaced save.
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        self.assertEqual(self.baseline(), before)
        self.job.local.write_bytes(database("merged"))
        self.assertEqual(self.job.synchronize("local"), "upload")
        self.assertEqual(self.remote.content, database("merged"))

    def test_late_local_creation_is_not_overwritten(self):
        self.job.local.unlink()
        rename = sync.rename_checked

        def create_then_install(source, destination, **kwargs):
            self.job.local.write_bytes(database("created-late"))
            rename(source, destination, **kwargs)

        with patch.object(sync, "rename_checked", side_effect=create_then_install):
            with self.assertRaises(sync.RetryLater):
                self.job.synchronize()
        self.assertEqual(self.job.local.read_bytes(), database("created-late"))
        self.assertFalse((self.job.state_dir / "baseline.json").exists())

    def test_exchange_keeps_inode_for_late_writes_and_prune_never_removes_it(self):
        self.establish()
        self.remote.content = database("remote-new")
        with self.job.local.open("r+b") as editor:
            self.assertEqual(self.job.synchronize(), "download")
            editor.write(database("late-write-on-open-file"))
            editor.truncate()
        saved = list((self.job.backups / "replaced").glob("*/*.kdbx"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), database("late-write-on-open-file"))
        self.config["max_backups"] = 1
        os.utime(saved[0], (1, 1))
        self.job.prune()
        self.assertTrue(saved[0].exists())

    def test_failure_after_exchange_keeps_displaced_file_and_baseline(self):
        self.establish()
        before = self.baseline()
        self.remote.content = database("remote-new")
        fsync = sync.fsync_directory

        def fail_after_exchange(path):
            if path == self.job.data:
                raise OSError("fsync failure after exchange")
            fsync(path)

        with patch.object(sync, "fsync_directory", side_effect=fail_after_exchange):
            with self.assertRaises(OSError):
                self.job.synchronize()
        saved = list((self.job.backups / "replaced").glob("*/*.kdbx"))
        self.assertEqual([p.read_bytes() for p in saved], [database("base")])
        self.assertEqual(self.baseline(), before)

    def test_unsupported_exchange_does_not_fall_back_to_overwrite(self):
        self.establish()
        self.remote.content = database("remote-new")
        with patch.object(sync.ctypes, "CDLL", return_value=object()):
            with self.assertRaises(sync.SyncError):
                self.job.synchronize()
        self.assertEqual(self.job.local.read_bytes(), database("base"))

    def test_just_saved_file_is_deferred(self):
        self.config["settle_seconds"] = 10
        with self.assertRaises(sync.RetryLater):
            self.job.synchronize()
        self.assertEqual(self.remote.fetches, 0)

    def test_invalid_local_and_remote_never_replace_other_copy(self):
        self.establish()
        self.remote.content = b"broken"
        with self.assertRaises(sync.SyncError):
            self.job.synchronize()
        self.assertEqual(self.job.local.read_bytes(), database("base"))
        self.job.local.write_bytes(b"broken-local")
        self.remote.content = database("base")
        with self.assertRaises(sync.SyncError):
            self.job.synchronize()
        self.assertEqual(self.remote.content, database("base"))

    def test_retention_count_age_and_protected_conflicts(self):
        self.establish()
        self.job.local.write_bytes(database("local-conflict"))
        self.remote.content = database("remote-conflict")
        with self.assertRaises(sync.Conflict):
            self.job.synchronize()
        conflicts = list((self.job.backups / "conflicts").glob("*/*.kdbx"))
        manual = self.job.backups / "manual.kdbx"
        manual.write_bytes(database("manual"))
        self.config["max_backups"] = 2
        for index in range(5):
            source = self.root / "fixture.kdbx"
            source.write_bytes(database(f"version-{index}"))
            self.job.backup(source, sync.digest(source))
        self.job.prune()
        self.assertEqual(len(self.versions()), 2)
        old = time.time() - 200 * 86400
        for path in self.versions():
            os.utime(path, (old, old))
        self.job.prune()
        self.assertEqual(len(self.versions()), 1)
        self.assertTrue(manual.exists())
        self.assertTrue(all(p.exists() for p in conflicts))

    def test_invalid_or_retargeted_baseline_does_not_transfer(self):
        self.establish()
        before = self.baseline()
        retargeted = json.loads(before)
        retargeted["identity"]["remote"] = "other:keepass/passwords.kdbx"
        for content in (b"{broken", b"[]", json.dumps(retargeted).encode()):
            (self.job.state_dir / "baseline.json").write_bytes(content)
            with self.assertRaises(sync.SyncError):
                self.job.synchronize()
        self.assertEqual(self.remote.uploads, 0)

    def test_local_execution_lock(self):
        with sync.execution_lock(self.job.state_dir):
            with self.assertRaises(sync.RetryLater):
                with sync.execution_lock(self.job.state_dir):
                    self.fail("Second lock was acquired")

    def test_atomic_copy_failure_keeps_destination(self):
        source = self.root / "source.kdbx"
        source.write_bytes(database("replacement"))
        with patch.object(sync.shutil, "copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sync.atomic_copy(source, self.job.local)
        self.assertEqual(self.job.local.read_bytes(), database("base"))
        self.assertFalse(list(self.job.data.glob(".write-*")))

    def test_corrupted_backup_stops_instead_of_reusing_it(self):
        self.establish()
        self.versions()[0].write_bytes(b"damaged-backup")
        with self.assertRaises(sync.SyncError):
            self.job.synchronize()
        self.assertEqual(self.remote.uploads, 0)


class ConfigAndNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(ROOT / "config.json", self.root / "config.json")

    def test_overrides_and_relative_config_path(self):
        sync.write_json(self.root / "config.local.json", {"retention_days": 7,
                                                         "rclone_config": ".state/rclone.conf"})
        config = sync.load_config(self.root)
        self.assertEqual(config["retention_days"], 7)
        self.assertEqual(config["rclone_config"], str(self.root / ".state/rclone.conf"))

    def test_credentials_inside_repository_must_be_in_state(self):
        for path in ("rclone.conf", "credentials.conf", ".state/../credentials.conf",
                     str(self.root / "credentials.conf"), ".state"):
            with self.subTest(path=path):
                sync.write_json(self.root / "config.local.json", {"rclone_config": path})
                with self.assertRaises(sync.SyncError):
                    sync.load_config(self.root)

    def test_credentials_outside_repository_are_allowed(self):
        with tempfile.TemporaryDirectory() as external:
            credentials = Path(external) / "rclone.conf"
            sync.write_json(self.root / "config.local.json", {"rclone_config": str(credentials)})
            self.assertEqual(sync.load_config(self.root)["rclone_config"], str(credentials))

    def test_credentials_symlink_to_unprotected_internal_path_is_rejected(self):
        (self.root / ".state").mkdir()
        (self.root / ".state/rclone.conf").symlink_to(self.root / "credentials.conf")
        sync.write_json(self.root / "config.local.json", {"rclone_config": ".state/rclone.conf"})
        with self.assertRaises(sync.SyncError):
            sync.load_config(self.root)

    def test_bad_configuration_rejected(self):
        for override in ({"retention_days": 0}, {"max_backups": True},
                         {"database": "../passwords.kdbx"}, {"notifications": "true"},
                         {"typo": 7}):
            sync.write_json(self.root / "config.local.json", override)
            with self.assertRaises(sync.SyncError):
                sync.load_config(self.root)

    def test_hostname_is_safe_for_filenames_and_bounded(self):
        for hostname, expected in (("elite", "elite"), ("../elite / laptop", "elite-laptop"),
                                   ("../", "unknown-host"), ("x" * 300, "x" * 64)):
            with patch.object(sync.socket, "gethostname", return_value=hostname):
                self.assertEqual(sync.host_label(), expected)

    def test_notification_uses_user_bus_and_cooldown(self):
        config = copy.deepcopy(DEFAULTS)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sync.shutil, "which", return_value="/usr/bin/notify-send"), \
                patch.object(sync.subprocess, "run", return_value=completed) as run, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(sync.Path, "exists", return_value=True), \
                patch.object(sync, "read_json", return_value={}):
            self.assertTrue(sync.notify(config, self.root, "Conflict"))
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                             f"unix:path=/run/user/{os.getuid()}/bus")
        with patch.object(sync.shutil, "which", return_value="/usr/bin/notify-send"), \
                patch.object(sync.subprocess, "run") as run:
            self.assertTrue(sync.notify(config, self.root, "Conflict"))
            run.assert_not_called()

    def test_missing_notifier_or_failed_delivery_not_marked_as_delivered(self):
        config = copy.deepcopy(DEFAULTS)
        with patch.object(sync.shutil, "which", return_value=None):
            self.assertFalse(sync.notify(config, self.root, "Conflict"))
        failure = subprocess.CompletedProcess([], 1, "", "no desktop session")
        with patch.object(sync.shutil, "which", return_value="notify-send"), \
                patch.object(sync.subprocess, "run", return_value=failure):
            self.assertFalse(sync.notify(config, self.root, "Conflict"))
        self.assertFalse((self.root / "notification.json").exists())

    def test_remote_duplicates_and_listing_errors_are_not_missing_file(self):
        transport = sync.Rclone(DEFAULTS)
        name = DEFAULTS["remote"].rsplit("/", 1)[-1]
        for listing in ([{"Name": name, "IsDir": False}] * 2,
                        [{"Name": name, "IsDir": True}]):
            with patch.object(transport, "run", return_value=json.dumps(listing)) as run:
                with self.assertRaises(sync.SyncError):
                    transport.fetch(self.root / "remote.kdbx")
                self.assertEqual(run.call_count, 1)
        with patch.object(transport, "run", side_effect=sync.SyncError("network failure")):
            with self.assertRaises(sync.SyncError):
                transport.fetch(self.root / "remote.kdbx")
        with patch.object(transport, "run", return_value="[]"):
            self.assertIsNone(transport.fetch(self.root / "remote.kdbx"))

    def test_fetch_preserves_remote_and_local_roots(self):
        for remote, parent in (("sftp:/passwords.kdbx", "sftp:/"),
                               ("sftp:passwords.kdbx", "sftp:"),
                               ("sftp:keepass/passwords.kdbx", "sftp:keepass/"),
                               ("/passwords.kdbx", "/"),
                               ("passwords.kdbx", "."),
                               (":local:/passwords.kdbx", ":local:/")):
            with self.subTest(remote=remote):
                transport = sync.Rclone(dict(DEFAULTS, remote=remote))

                def run(*args):
                    if args[0] == "lsjson":
                        self.assertEqual(args[1], parent)
                        return '[{"Name": "passwords.kdbx", "IsDir": false}]'
                    self.assertEqual(args[0:2], ("copyto", remote))
                    Path(args[2]).write_bytes(database("existing-remote"))

                with patch.object(transport, "run", side_effect=run):
                    destination = self.root / "remote.kdbx"
                    self.assertEqual(transport.fetch(destination), sync.digest(destination))

    def test_first_sync_root_remote_preserves_existing_different_database(self):
        config = dict(DEFAULTS, remote="sftp:/passwords.kdbx", settle_seconds=0)
        transport = sync.Rclone(config)
        job = sync.Synchronizer(self.root, config, transport)
        job.local.write_bytes(database("local"))

        def run(*args):
            if args[0] == "lsjson":
                return ('[{"Name": "passwords.kdbx", "IsDir": false}]'
                        if args[1] == "sftp:/" else "[]")
            self.assertEqual(args[1], config["remote"], "Unexpected upload")
            Path(args[2]).write_bytes(database("existing-root-remote"))

        with patch.object(transport, "run", side_effect=run):
            with self.assertRaises(sync.Conflict):
                job.synchronize()
        self.assertEqual(job.local.read_bytes(), database("local"))
        self.assertIn(database("existing-root-remote"),
                      {p.read_bytes() for p in job.backups.glob("version-*.kdbx")})


@unittest.skipUnless(shutil.which("rclone"), "rclone not installed")
class RealRcloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keepass-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repository with spaces"
        self.root.mkdir()
        shutil.copyfile(ROOT / "i18n.py", self.root / "i18n.py")
        shutil.copytree(ROOT / "locales", self.root / "locales")
        self.remote_dir = Path(self.temp.name) / "remote"
        self.remote_dir.mkdir()
        self.remote_file = self.remote_dir / DEFAULTS["database"]
        self.config = copy.deepcopy(DEFAULTS)
        self.config.update(remote=str(self.remote_file), rclone=shutil.which("rclone"),
                           settle_seconds=0, notifications=False)
        self.job = sync.Synchronizer(self.root, self.config)
        self.job.local.write_bytes(database("initial"))

    def test_actual_copyto_upload_download_and_unrelated_files(self):
        unrelated = self.remote_dir / "unrelated.txt"
        unrelated.write_text("keep this")
        self.assertEqual(self.job.synchronize(), "upload")
        self.assertEqual(self.remote_file.read_bytes(), database("initial"))
        self.remote_file.write_bytes(database("remote-edited"))
        self.assertEqual(self.job.synchronize(), "download")
        self.assertEqual(self.job.local.read_bytes(), database("remote-edited"))
        self.assertEqual(unrelated.read_text(), "keep this")
        self.assertEqual(self.job.synchronize(), "unchanged")

    def test_cli_from_other_directory_and_symlink(self):
        shutil.copyfile(ROOT / "sync.py", self.root / "sync.py")
        (self.root / "sync.py").chmod(0o755)
        sync.write_json(self.root / "config.json", self.config)
        link = Path(self.temp.name) / "entry.py"
        link.symlink_to(self.root / "sync.py")
        result = subprocess.run([str(link)], cwd="/", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_file.read_bytes(), database("initial"))
        self.assertTrue((self.root / ".state" / "baseline.json").exists())
        status = subprocess.run([sys.executable, str(link), "--status"], cwd="/",
                                capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)["status"], "ok")
        self.assertEqual((self.root / ".state" / "baseline.json").stat().st_mode & 0o777, 0o600)

    def test_cli_conflict_exit_status_and_protected_copies(self):
        self.remote_file.write_bytes(database("different"))
        shutil.copyfile(ROOT / "sync.py", self.root / "sync.py")
        sync.write_json(self.root / "config.json", self.config)
        result = subprocess.run([sys.executable, str(self.root / "sync.py")],
                                cwd="/", capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(sync.read_json(self.root / ".state" / "status.json")["status"], "conflict")
        self.assertEqual(self.remote_file.read_bytes(), database("different"))
        self.assertEqual(self.job.local.read_bytes(), database("initial"))


if __name__ == "__main__":
    unittest.main()
