from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sms_sender_web.backup import list_backups, verify


class Command(BaseCommand):
    help = (
        "Check a backup: every file present, unchanged (SHA-256), and every "
        "DB whole, with the row counts it had. Default: the newest backup."
    )

    def add_arguments(self, parser):
        parser.add_argument("backup", nargs="?", type=Path, default=None)

    def handle(self, *args, backup: Path | None, **options):
        if backup is None:
            backups = list_backups(settings.BACKUP_DIR)
            if not backups:
                raise CommandError(f"no backup in {settings.BACKUP_DIR}")
            backup = backups[-1]
        problems = verify(backup)
        if problems:
            for problem in problems:
                self.stderr.write(problem)
            raise CommandError(f"{backup} is damaged ({len(problems)} problem(s))")
        self.stdout.write(f"OK: {backup} is whole")
