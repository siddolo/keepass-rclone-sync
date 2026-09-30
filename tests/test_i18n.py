import argparse
import ast
import gettext
import json
import os
from pathlib import Path
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import i18n
import sync


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, "en")
        self.temp = tempfile.TemporaryDirectory(prefix="keepass-i18n-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads((ROOT / "config.json").read_text())
        self.config.update(notifications=False, settle_seconds=0)
        for name in ("sync.py", "manage.py", "i18n.py", "Makefile"):
            shutil.copyfile(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "locales", self.root / "locales")
        sync.write_json(self.root / "config.json", self.config)
        self.state = self.root / ".state"
        self.state.mkdir()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", LANG="it_IT.UTF-8",
                        LANGUAGE="it", LC_ALL="C")

    def cli(self, script, *arguments):
        return subprocess.run([sys.executable, str(self.root / script), *arguments],
                              cwd="/", env=self.env, capture_output=True, text=True, timeout=20)

    def test_default_is_english_even_with_italian_environment(self):
        for script in ("sync.py", "manage.py"):
            result = self.cli(script, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)
            self.assertIn("show this help message and exit", result.stdout)

    def test_config_language_and_cli_override_apply_to_help(self):
        sync.write_json(self.root / "config.local.json", {"language": "it"})
        for script in ("sync.py", "manage.py"):
            result = self.cli(script, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("uso:", result.stdout)
            self.assertIn("mostra questo messaggio", result.stdout)
            result = self.cli(script, "--language", "en", "--help")
            self.assertIn("usage:", result.stdout)
            self.assertIn("show this help message", result.stdout)

    def test_manage_language_works_before_or_after_subcommand(self):
        for arguments in (("--language", "it", "config", "--help"),
                          ("config", "--language", "it", "--help")):
            result = self.cli("manage.py", *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("uso:", result.stdout)
            self.assertIn("Lingua dei messaggi", result.stdout)

    def test_cli_errors_and_invalid_configuration_are_localized(self):
        result = self.cli("sync.py", "--language", "it", "--resolve", "invalid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("scelta non valida", result.stderr)
        (self.root / "config.local.json").write_text("{broken")
        result = self.cli("sync.py", "--language", "it", "--status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Errore di avvio", result.stderr)
        self.assertIn("Impossibile leggere", result.stderr)
        result = self.cli("sync.py", "--status")
        self.assertIn("Startup error", result.stderr)
        self.assertIn("Cannot read", result.stderr)

    def test_language_validation_and_older_configuration(self):
        self.config.pop("language")
        sync.write_json(self.root / "config.json", self.config)
        self.assertEqual(sync.load_config(self.root)["language"], "en")
        sync.write_json(self.root / "config.local.json", {"language": "it"})
        self.assertEqual(sync.load_config(self.root)["language"], "it")
        for language in ("fr", "auto", None, [], True):
            sync.write_json(self.root / "config.local.json", {"language": language})
            with self.assertRaises(sync.SyncError):
                sync.load_config(self.root)

    def test_nested_messages_round_trip_in_another_language(self):
        i18n.set_language("it")
        reason = i18n._("Corrupted backup: {path}").format(path="data/a {b}.kdbx")
        message = i18n._("{reason}. Copies preserved in {directory}").format(
            reason=reason, directory="backup/with spaces")
        record = json.loads(json.dumps({"message": message, **i18n.message_fields(message)}))
        self.assertIn("Backup danneggiato", record["message"])
        i18n.set_language("en")
        rendered = i18n.render(record)
        self.assertEqual(rendered, "Corrupted backup: data/a {b}.kdbx. Copies preserved in backup/with spaces")
        self.assertEqual(i18n.message_fields(rendered), i18n.message_fields(message))

    def test_status_renders_stored_error_in_selected_language_without_rewriting(self):
        i18n.set_language("it")
        message = i18n._("Corrupted backup: {path}").format(path="data/backup/a.kdbx")
        with patch.object(sync, "notify"), self.assertLogs(sync.LOG, level="ERROR"):
            self.assertEqual(sync.report_error(self.config, self.state, sync.SyncError(message)), 1)
        before = (self.state / "status.json").read_bytes()
        result = self.cli("sync.py", "--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["message"], "Corrupted backup: data/backup/a.kdbx")
        result = self.cli("sync.py", "--status", "--language", "it")
        self.assertEqual(json.loads(result.stdout)["message"], "Backup danneggiato: data/backup/a.kdbx")
        self.assertEqual((self.state / "status.json").read_bytes(), before)

    def test_legacy_status_and_conflict_still_work(self):
        sync.write_json(self.state / "status.json", {"status": "error", "message": "Errore precedente"})
        result = self.cli("sync.py", "--status", "--language", "en")
        self.assertEqual(json.loads(result.stdout)["message"], "Errore precedente")
        sync.write_json(self.state / "conflict.json", {
            "reason": "Conflitto precedente", "requires_resolution": True})
        result = self.cli("sync.py", "--language", "it")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Conflitto precedente. Unire la copia conservata", result.stderr)

    def test_conflict_reason_is_rendered_in_current_language(self):
        i18n.set_language("it")
        reason = i18n._("Concurrent local save: original preserved at {path}").format(path="backup/a.kdbx")
        job = sync.Synchronizer(self.root, self.config)
        job.preserve_conflict(None, None, None, None, reason, requires_resolution=True)
        result = self.cli("sync.py", "--language", "en")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Concurrent local save: original preserved at backup/a.kdbx", result.stderr)
        self.assertIn("Merge the preserved copy", result.stderr)

    def test_success_and_deferral_store_translatable_messages(self):
        args = argparse.Namespace(status=False, test_notification=False, resolve=None)
        for language in ("en", "it"):
            i18n.set_language(language)
            with patch.object(sync.Synchronizer, "synchronize", return_value="upload"):
                self.assertEqual(sync.run_job(self.root, self.config, args), 0)
            record = sync.read_json(self.state / "status.json")
            i18n.set_language("en")
            self.assertEqual(i18n.render(record), "Database uploaded and verified")
            self.assertEqual(record["action"], "upload")
        i18n.set_language("it")
        with patch.object(sync.Synchronizer, "synchronize", side_effect=sync.RetryLater(
                i18n._("Database changed while being read"))):
            self.assertEqual(sync.run_job(self.root, self.config, args), 0)
        i18n.set_language("en")
        self.assertEqual(i18n.render(sync.read_json(self.state / "status.json")),
                         "Database changed while being read")

    def test_notification_title_and_cooldown_across_language_changes(self):
        self.config["notifications"] = True
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sync.shutil, "which", return_value="notify-send"), \
                patch.object(sync.subprocess, "run", return_value=completed) as run:
            i18n.set_language("it")
            message = i18n._("Corrupted backup: {path}").format(path="data/test.kdbx")
            self.assertTrue(sync.notify(self.config, self.state, message))
            self.assertEqual(run.call_args.args[0][-2], "KeePass: sincronizzazione")
            self.assertEqual(run.call_args.args[0][-1], "Backup danneggiato: data/test.kdbx")
            i18n.set_language("en")
            message = i18n._("Corrupted backup: {path}").format(path="data/test.kdbx")
            self.assertTrue(sync.notify(self.config, self.state, message))
            self.assertEqual(run.call_count, 1)
            self.assertTrue(sync.notify(self.config, self.state, message, force=True))
            self.assertEqual(run.call_args.args[0][-2], "KeePass: synchronization")

    def test_missing_catalog_falls_back_to_english(self):
        with patch.object(i18n, "LOCALES", self.root / "missing"):
            i18n.set_language("it")
            self.assertEqual(i18n._("Database uploaded and verified"), "Database uploaded and verified")

    def test_catalog_covers_source_messages_and_preserves_placeholders(self):
        catalog = gettext.translation("keepass-sync", ROOT / "locales", languages=["it"])
        formatter = string.Formatter()
        for filename in ("sync.py", "manage.py", "i18n.py"):
            tree = ast.parse((ROOT / filename).read_text())
            for node in ast.walk(tree):
                if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                        or node.func.id not in ("_", "N_") or not node.args):
                    continue
                self.assertIsInstance(node.args[0], ast.Constant, "Translate templates, not interpolated strings")
                template = node.args[0].value
                translated = catalog.gettext(template)
                self.assertNotEqual(translated, template, f"Missing Italian translation: {template}")
                fields = lambda text: {field for _, field, _, _ in formatter.parse(text) if field is not None}
                self.assertEqual(fields(template), fields(translated), template)

    @unittest.skipUnless(shutil.which("msgfmt"), "GNU gettext not installed")
    def test_compiled_catalog_matches_po_and_passes_gettext_checks(self):
        result = subprocess.run(["msgfmt", "--check", "--check-format", "-o", "-",
                                 str(ROOT / "locales/it/LC_MESSAGES/keepass-sync.po")],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, (ROOT / "locales/it/LC_MESSAGES/keepass-sync.mo").read_bytes())

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_make_language_override(self):
        result = subprocess.run(["make", "help", "UI_LANGUAGE=it"], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Comandi di gestione", result.stdout)


if __name__ == "__main__":
    unittest.main()
