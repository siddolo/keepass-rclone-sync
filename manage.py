#!/usr/bin/env python3
"""Management commands for configuration, rclone, and cron."""

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import sync
import i18n
from i18n import _


ROOT = Path(__file__).resolve().parent
BEGIN = "# BEGIN keepass-rclone-sync"
END = "# END keepass-rclone-sync"
DEFAULT_SCHEDULE = "*/15 * * * *"


def run(command, **kwargs):
    try:
        return subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise sync.SyncError(_("Cannot run {command}: {error}").format(command=command[0], error=str(exc))) from exc


def edit_config(root):
    path = root / "config.local.json"
    if not path.exists():
        sync.write_json(path, {})
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "editor")
    if not editor:
        raise sync.SyncError(_("Set VISUAL or EDITOR to an available editor"))
    result = run([*editor, str(path)])
    if result.returncode:
        raise sync.SyncError(_("Editor exited with code {code}").format(code=result.returncode))
    sync.load_config(root)
    print(_("Valid configuration: {path} (overrides config.json)").format(path=str(path)))
    return 0


def authenticate(root):
    config = sync.load_config(root)
    print(_("Configure the provider and credentials for the remote used by {remote}").format(remote=config['remote']), flush=True)
    command = [config["rclone"], "config"]
    if config["rclone_config"]:
        credentials = Path(config["rclone_config"])
        if credentials.is_relative_to(root.resolve() / ".state"):
            (root / ".state").mkdir(mode=0o700, exist_ok=True)
            credentials.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        command += ["--config", config["rclone_config"]]
    return run(command).returncode


def validate_schedule(schedule):
    fields = schedule.split()
    if len(fields) != 5:
        raise sync.SyncError(_("The cron schedule must contain five fields"))
    for field, (minimum, maximum) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        for part in field.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
            if not match or (match[2] is not None and int(match[2]) < 1):
                raise sync.SyncError(_("Invalid cron field: {field}").format(field=field))
            if match[1] != "*":
                bounds = [int(value) for value in match[1].split("-")]
                if any(value < minimum or value > maximum for value in bounds) or bounds != sorted(bounds):
                    raise sync.SyncError(_("Cron field out of range: {field}").format(field=field))
    return " ".join(fields)


def cron_command(root):
    paths = (str(Path(sys.executable).resolve()), str(root.resolve() / "sync.py"))
    if any("\n" in path or "\r" in path for path in paths):
        raise sync.SyncError(_("The repository path cannot contain newlines"))
    # Cron interprets % even inside quotes, so escape it separately.
    return " ".join(shlex.quote(path) for path in paths).replace("%", r"\%")


def read_crontab():
    env = dict(os.environ, LC_ALL="C")
    result = run(["crontab", "-l"], capture_output=True, text=True, env=env, timeout=20)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and result.stderr.strip().lower().startswith("no crontab for "):
        return ""
    raise sync.SyncError(_("Cannot read crontab: {error}").format(error=result.stderr.strip()))


def split_managed(text):
    other, managed = [], []
    inside = False
    for line in text.splitlines():
        if line == BEGIN:
            if inside:
                raise sync.SyncError(_("Invalid managed cron block: nested BEGIN marker"))
            inside = True
        elif line == END:
            if not inside:
                raise sync.SyncError(_("Invalid managed cron block: unmatched END marker"))
            inside = False
        elif inside:
            managed.append(line)
        else:
            other.append(line)
    if inside:
        raise sync.SyncError(_("Incomplete managed cron block: missing END"))
    return other, managed


def parse_job(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    fields = line.split(None, 5)
    if len(fields) == 6:
        try:
            return validate_schedule(" ".join(fields[:5])), fields[5]
        except sync.SyncError:
            pass
    return None


def is_our_job(line, root):
    job = parse_job(line)
    if not job:
        return False
    if job[1] == cron_command(root):
        return True
    try:
        command = shlex.split(job[1].replace(r"\%", "%"))
    except ValueError:
        return False
    script = str(root.resolve() / "sync.py")
    # Also recognize the documented manual entry that runs the script directly.
    return (command == [script] or
            (len(command) == 2 and command[1] == script
             and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(command[0]).name)))


def build_crontab(current, root, schedule):
    schedule = validate_schedule(schedule)
    other, _managed = split_managed(current)
    other = [line for line in other if not is_our_job(line, root)]
    block = [BEGIN, f"{schedule} {cron_command(root)}", END]
    return "\n".join(other + block) + "\n"


def setup_cron(root, schedule):
    schedule = validate_schedule(schedule)
    sync.load_config(root)
    script = root / "sync.py"
    if not script.is_file():
        raise sync.SyncError(_("Script not found: {path}").format(path=str(script)))
    state = root / ".state"
    state.mkdir(mode=0o700, exist_ok=True)
    with sync.execution_lock(state):
        current = read_crontab()
        updated = build_crontab(current, root, schedule)
        if current == updated:
            print(_("Cron is already configured: no changes"))
            return 0
        # Keep one backup of the last replaced configuration outside Git.
        backup = state / "crontab.before-setup.txt"
        backup.write_text(current)
        if read_crontab() != current:
            raise sync.SyncError(_("Crontab changed concurrently: run setup again"))
        result = run(["crontab", "-"], input=updated, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise sync.SyncError(_("Cannot install crontab: {error}").format(error=result.stderr.strip()))
        print(_("Cron installed: {schedule} {command}").format(schedule=validate_schedule(schedule), command=cron_command(root)))
        print(_("Previous crontab saved to {path}").format(path=str(backup)))
    return 0


def cron_running():
    # Linux implementation that works without systemd or a ps/pgrep dependency.
    for process in Path("/proc").iterdir():
        if process.name.isdigit():
            try:
                if (process / "comm").read_text().strip() in ("cron", "crond"):
                    return True
            except OSError:
                continue
    return False


def check_cron(root):
    current = read_crontab()
    other, managed = split_managed(current)
    jobs = [parse_job(line) for line in managed if line.strip() and not line.startswith("#")]
    if len(jobs) != 1 or not jobs[0] or jobs[0][1] != cron_command(root):
        raise sync.SyncError(_("Managed job missing or using a different path: run make cron-setup"))
    if any(is_our_job(line, root) for line in other):
        raise sync.SyncError(_("Duplicate job outside the managed block: remove the duplicate entry"))
    if not cron_running():
        raise sync.SyncError(_("The cron/crond daemon is not running"))
    return _("job {schedule}, daemon running").format(schedule=jobs[0][0])


def general_check(root):
    failures = 0

    def report(name, operation):
        nonlocal failures
        try:
            detail = operation()
            print(f"[OK] {name}: {detail}")
            return detail
        except (sync.SyncError, OSError, ValueError, KeyError, TypeError) as exc:
            failures += 1
            print(_("[ERROR] {name}: {error}").format(name=name, error=str(exc)))
            return None

    config = None

    def configuration():
        nonlocal config
        config = sync.load_config(root)
        return config["remote"]

    report(_("Configuration"), configuration)
    report("Cron", lambda: check_cron(root))
    if config:
        transport = sync.Rclone(config)
        report("rclone", lambda: transport.run("version").splitlines()[0])
        local = root / "data" / config["database"]

        def local_check():
            sync.validate_kdbx(local)
            for directory in (root / "data", root / "data" / "backup", root / ".state"):
                parent = directory if directory.exists() else directory.parent
                if not os.access(parent, os.W_OK | os.X_OK):
                    raise sync.SyncError(_("Directory is not writable: {path}").format(path=str(directory)))
            return local.name

        report(_("Local database and directories"), local_check)

        def remote_check():
            with tempfile.TemporaryDirectory(prefix="keepass-check-") as directory:
                value = transport.fetch(Path(directory) / "remote.kdbx")
            if value is None:
                raise sync.SyncError(_("Authentication/listing succeeded, but the remote database is missing"))
            return _("authenticated access, download and KDBX header verified")

        report(_("rclone remote access"), remote_check)
        if config["notifications"] and not shutil.which("notify-send"):
            print(_("[WARNING] Desktop notifications unavailable: install libnotify-bin"))

        def last_result():
            conflict = sync.read_json(root / ".state" / "conflict.json")
            if conflict:
                raise sync.SyncError(_("Unresolved conflict: see .state/conflict.json"))
            status = sync.read_json(root / ".state" / "status.json")
            if not status:
                return _("no synchronization recorded yet")
            if status.get("status") in ("error", "conflict"):
                raise sync.SyncError(i18n.render(status, status["status"]))
            recorded_at = status.get("time", _("unknown date"))
            return f"{status.get('status')} ({recorded_at})"

        report(_("Last synchronization result"), last_result)
    print(_("Check complete. Errors: {count}").format(count=failures))
    return 1 if failures else 0


def main():
    language = i18n.select_cli_language(sys.argv[1:])
    try:
        config = sync.load_config(ROOT)
    except (sync.SyncError, OSError, ValueError, KeyError, TypeError):
        config = None
    if config and not language:
        i18n.set_language(config["language"])
    i18n.configure_argparse()
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=_("Management commands for configuration, rclone, and cron."),
        epilog=_("Make targets: config, auth, sync, cron-setup, check, status, test."),
    )
    i18n.add_language_option(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", allow_abbrev=False, help=_("Edit config.local.json using VISUAL/EDITOR"))
    commands.add_parser("auth", allow_abbrev=False, help=_("Run rclone config to select the provider and configure credentials"))
    cron = commands.add_parser("cron-setup", allow_abbrev=False, help=_("Install or update the user's cron job"))
    cron.add_argument("--schedule", default=DEFAULT_SCHEDULE)
    commands.add_parser("check", allow_abbrev=False, help=_("Check configuration, cron, rclone and access to the remote database"))
    for subparser in commands.choices.values():
        i18n.add_language_option(subparser, suppress_default=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "config":
            return edit_config(ROOT)
        if args.command == "auth":
            return authenticate(ROOT)
        if args.command == "cron-setup":
            return setup_cron(ROOT, args.schedule)
        return general_check(ROOT)
    except sync.RetryLater as exc:
        print(_("Operation deferred: {reason}").format(reason=str(exc)), file=sys.stderr)
        return 1
    except (sync.SyncError, OSError, ValueError, KeyError, TypeError) as exc:
        print(_("Error: {error}").format(error=str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
