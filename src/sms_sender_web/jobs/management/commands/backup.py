from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sms_sender_web.backup import BackupError, make_backup


class Command(BaseCommand):
    help = (
        "Back up the app DB, every campaign DB and the segment files into "
        "<backups>/<UTC time>/, then keep only the newest ones. Safe while "
        "the dashboard and a send are running. The backup holds phone "
        "numbers: copy it off the machine only encrypted."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dest", type=Path, default=None,
                            help="Default: SMS_SENDER_BACKUP_DIR, else <data>/backups.")
        parser.add_argument("--keep", type=int, default=14,
                            help="How many backups to keep (default 14).")

    def handle(self, *args, dest: Path | None, keep: int, **options):
        dest = dest or settings.BACKUP_DIR
        try:
            result = make_backup(Path(settings.DATA_DIR), dest, keep=keep)
        except (BackupError, OSError) as e:
            raise CommandError(f"backup failed, nothing kept: {e}") from e
        self.stdout.write(
            f"Backed up {result.files} files ({result.bytes / 1e6:.1f} MB) to {result.path}"
        )
        for path in result.pruned:
            self.stdout.write(f"removed old backup {path.name}")
