import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keepass-manage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()
        for name in ("config.json", "sync.py"):
            shutil.copyfile(ROOT / name, self.root / name)

    def test_config_edits_local_overrides_and_validates_them(self):
        with patch.dict(os.environ, {"VISUAL": "my-editor --wait", "EDITOR": "other-editor"}), \
                patch.object(manage, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            manage.edit_config(self.root)
        self.assertEqual(json.loads((self.root / "config.local.json").read_text()), {})
        self.assertEqual(run.call_args.args[0], ["my-editor", "--wait", str(self.root / "config.local.json")])
        (self.root / "config.local.json").write_text('{"retention_days": 0}')
        with patch.object(manage, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(manage.sync.SyncError):
                manage.edit_config(self.root)

    def test_auth_uses_configured_binary_and_config_file(self):
        (self.root / "config.local.json").write_text(json.dumps({
            "rclone": "/custom/bin/rclone", "rclone_config": ".state/rclone.conf",
            "remote": "sftp:keepass/passwords.kdbx",
        }))
        with patch.object(manage, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manage.authenticate(self.root), 0)
        self.assertEqual(run.call_args.args[0], ["/custom/bin/rclone", "config", "--config",
                                               str(self.root / ".state/rclone.conf")])
        self.assertEqual((self.root / ".state").stat().st_mode & 0o777, 0o700)
        self.assertIn("sftp:keepass/passwords.kdbx", output.getvalue())

    def test_auth_rejects_unprotected_credentials_before_running_rclone(self):
        (self.root / "config.local.json").write_text(json.dumps({"rclone_config": "credentials.conf"}))
        with patch.object(manage, "run") as run:
            with self.assertRaises(manage.sync.SyncError):
                manage.authenticate(self.root)
        run.assert_not_called()
        self.assertFalse((self.root / "credentials.conf").exists())

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_supported_internal_credentials_are_ignored_by_git(self):
        shutil.copyfile(ROOT / ".gitignore", self.root / ".gitignore")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True,
                       capture_output=True, text=True)
        (self.root / "config.local.json").write_text(json.dumps({"rclone_config": ".state/rclone.conf"}))
        credentials = Path(manage.sync.load_config(self.root)["rclone_config"])
        credentials.parent.mkdir(mode=0o700)
        credentials.write_text("synthetic credentials")
        result = subprocess.run(["git", "check-ignore", "--", str(credentials)],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_schedule_validates_ranges_lists_steps_and_limits(self):
        self.assertEqual(manage.validate_schedule("  */15  * * * * "), "*/15 * * * *")
        self.assertEqual(manage.validate_schedule("0,30 8-18/2 1-31 1-12 0-7"),
                         "0,30 8-18/2 1-31 1-12 0-7")
        for schedule in ("* * *", "*/0 * * * *", "60 * * * *", "0 24 * * *",
                         "0 1 0 * *", "0 1 * 13 *", "0 1 * * 8", "10-5 * * * *",
                         "@reboot", "* * * * * command"):
            with self.assertRaises(manage.sync.SyncError):
                manage.validate_schedule(schedule)

    def test_build_crontab_preserves_jobs_and_is_idempotent(self):
        unrelated = "# user jobs\nMAILTO=user@example.com\n0 4 * * * /usr/bin/backup\n@daily /usr/bin/other\n"
        result = manage.build_crontab(unrelated, self.root, "*/15 * * * *")
        self.assertTrue(result.startswith(unrelated))
        self.assertEqual(result.count(manage.BEGIN), 1)
        self.assertEqual(manage.build_crontab(result, self.root, "*/15 * * * *"), result)
        changed = manage.build_crontab(result, self.root, "*/30 * * * *")
        self.assertIn("*/30 * * * *", changed)
        self.assertNotIn("*/15 * * * *", changed)
        self.assertTrue(changed.startswith(unrelated))

    def test_setup_replaces_manual_job_for_same_script_only(self):
        other = "*/5 * * * * /other/repository/sync.py\n"
        script = str(self.root / "sync.py")
        for command in (shlex.quote(script), f"/usr/bin/python3 {shlex.quote(script)}"):
            current = other + f"*/15 * * * * {command}\n"
            result = manage.build_crontab(current, self.root, "*/15 * * * *")
            self.assertEqual(result.count(script), 1)
            self.assertTrue(result.startswith(other))

    def test_cron_path_quotes_spaces_and_escapes_percent(self):
        root = self.root / "100% complete"
        command = manage.cron_command(root)
        self.assertIn(r"100\% complete", command)
        parsed = shlex.split(command.replace(r"\%", "%"))
        self.assertEqual(parsed, [str(Path(sys.executable).resolve()), str(root / "sync.py")])

    def test_unbalanced_markers_abort_without_dropping_user_jobs(self):
        for text in (manage.BEGIN + "\n0 4 * * * /user/job\n", manage.END + "\n",
                     manage.BEGIN + "\n" + manage.BEGIN + "\n" + manage.END + "\n"):
            with self.assertRaises(manage.sync.SyncError):
                manage.build_crontab(text, self.root, "*/15 * * * *")

    def test_no_crontab_and_permission_failure_are_distinct(self):
        absent = subprocess.CompletedProcess([], 1, "", "no crontab for user\n")
        denied = subprocess.CompletedProcess([], 1, "", "permission denied\n")
        with patch.object(manage, "run", return_value=absent):
            self.assertEqual(manage.read_crontab(), "")
        with patch.object(manage, "run", return_value=denied):
            with self.assertRaises(manage.sync.SyncError):
                manage.read_crontab()

    def test_setup_stops_if_crontab_changes_before_install(self):
        with patch.object(manage, "read_crontab", side_effect=["", "0 4 * * * /new/job\n"]), \
                patch.object(manage, "run") as run:
            with self.assertRaises(manage.sync.SyncError):
                manage.setup_cron(self.root, "*/15 * * * *")
            run.assert_not_called()

    def test_check_cron_detects_missing_job_duplicate_or_inactive_daemon(self):
        installed = manage.build_crontab("", self.root, "*/15 * * * *")
        with patch.object(manage, "read_crontab", return_value=installed), \
                patch.object(manage, "cron_running", return_value=True):
            self.assertIn("daemon running", manage.check_cron(self.root))
        manual = f"*/15 * * * * {shlex.quote(str(self.root / 'sync.py'))}\n"
        for table, active in (("", True), (installed + manual, True), (installed, False)):
            with patch.object(manage, "read_crontab", return_value=table), \
                    patch.object(manage, "cron_running", return_value=active):
                with self.assertRaises(manage.sync.SyncError):
                    manage.check_cron(self.root)


@unittest.skipUnless(shutil.which("make"), "make not installed")
class MakeQuotingTests(unittest.TestCase):
    def test_repository_names_are_not_shell_programs(self):
        with tempfile.TemporaryDirectory(prefix="keepass-make-quoting-") as temporary:
            base = Path(temporary)
            for name in ("repo-$(touch injected-substitution)",
                         "repo-`touch injected-backtick`",
                         "repo 'single' \"double\" $literal; & (brackets)"):
                with self.subTest(name=name):
                    root = base / name
                    root.mkdir()
                    for file in ("Makefile", "manage.py", "sync.py", "i18n.py", "config.json"):
                        shutil.copyfile(ROOT / file, root / file)
                    for target in ("help", "status"):
                        result = subprocess.run(
                            ["make", "--no-print-directory", "-f", str(root / "Makefile"), target],
                            cwd=base, capture_output=True, text=True, timeout=20,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse((base / "injected-substitution").exists())
                    self.assertFalse((base / "injected-backtick").exists())

    def test_schedule_is_validated_as_data_without_shell_execution(self):
        with tempfile.TemporaryDirectory(prefix="keepass-make-schedule-") as temporary:
            root = Path(temporary)
            for file in ("Makefile", "manage.py", "sync.py", "i18n.py", "config.json"):
                shutil.copyfile(ROOT / file, root / file)
            result = subprocess.run(
                ["make", "cron-setup", 'CRON_SCHEDULE=*/15 * * * * `touch injected-schedule`'],
                cwd=root, capture_output=True, text=True, timeout=20,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("five fields", result.stderr)
            self.assertFalse((root / "injected-schedule").exists())


@unittest.skipUnless(shutil.which("make") and shutil.which("rclone"), "make/rclone not installed")
class MakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keepass-make-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repository with spaces"
        self.root.mkdir()
        for name in ("Makefile", "manage.py", "sync.py", "i18n.py", "config.json"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.remote_dir = Path(self.temp.name) / "remote"
        self.remote_dir.mkdir()
        config = json.loads((self.root / "config.json").read_text())
        self.remote = self.remote_dir / config["database"]
        config.update(remote=str(self.remote), rclone=shutil.which("rclone"),
                      notifications=False, settle_seconds=0)
        (self.root / "config.json").write_text(json.dumps(config))
        self.data = self.root / "data"
        self.data.mkdir()
        self.local = self.data / config["database"]
        self.local.write_bytes(manage.sync.KDBX_SIGNATURE + b"\x00\x00\x04\x00fixture")
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.table = Path(self.temp.name) / "crontab.txt"
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_CRONTAB_FILE=str(self.table))
        fake = self.bin / "crontab"
        fake.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import sys
table = Path(os.environ['FAKE_CRONTAB_FILE'])
if sys.argv[1:] == ['-l']:
    if table.exists():
        sys.stdout.write(table.read_text())
    else:
        sys.stderr.write('no crontab for test-user\\n')
        sys.exit(1)
elif sys.argv[1:] == ['-']:
    table.write_text(sys.stdin.read())
else:
    sys.exit(2)
""")
        fake.chmod(0o755)

    def make(self, target, *arguments):
        return subprocess.run(["make", "--no-print-directory", "-f", str(self.root / "Makefile"),
                               target, *arguments], cwd="/", env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_make_sync_runs_from_another_directory(self):
        result = self.make("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote.read_bytes(), self.local.read_bytes())

    def test_make_cron_setup_uses_fake_crontab_preserves_jobs_and_updates_schedule(self):
        original = "# user jobs\n0 4 * * * /usr/bin/backup\n"
        self.table.write_text(original)
        first = self.make("cron-setup")
        self.assertEqual(first.returncode, 0, first.stderr)
        installed = self.table.read_text()
        self.assertTrue(installed.startswith(original))
        self.assertEqual((self.root / ".state" / "crontab.before-setup.txt").read_text(), original)
        again = self.make("cron-setup")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.table.read_text(), installed)
        self.assertEqual((self.root / ".state" / "crontab.before-setup.txt").read_text(), original)
        changed = self.make("cron-setup", "CRON_SCHEDULE=*/30 * * * *")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertIn("*/30 * * * *", self.table.read_text())
        self.assertEqual(self.table.read_text().count(manage.BEGIN), 1)

    def test_make_cron_setup_handles_first_crontab(self):
        result = self.make("cron-setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(manage.BEGIN, self.table.read_text())

    def test_make_help_lists_targets(self):
        result = self.make("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sync, cron-setup, check", result.stdout)

    def test_general_check_downloads_without_mutating_database_backup_or_state(self):
        self.remote.write_bytes(self.local.read_bytes())
        (self.data / "backup").mkdir()
        installed = manage.build_crontab("", self.root, "*/15 * * * *")
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with patch.object(manage, "read_crontab", return_value=installed), \
                patch.object(manage, "cron_running", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manage.general_check(self.root), 0, output.getvalue())
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertIn("download and KDBX header verified", output.getvalue())

    def test_general_check_reports_cron_and_remote_failures(self):
        with patch.object(manage, "read_crontab", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manage.general_check(self.root), 1)
        self.assertIn("[ERROR] Cron", output.getvalue())
        self.assertIn("[ERROR] rclone remote access", output.getvalue())


if __name__ == "__main__":
    unittest.main()
