from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sms_sender_web.backup import BackupError, restore


class Command(BaseCommand):
    help = (
        "Put a backup's files back into the data folder. Stop the dashboard "
        "and the worker first. Files that are missing are restored; an "
        "existing file only when named with --replace, and it's moved aside "
        "(<name>.before-restore-<time>), never deleted. A campaign DB from "
        "before a send has no record of it: don't resume that campaign from "
        "the restored copy (docs/deploy.md)."
    )

    def add_arguments(self, parser):
        parser.add_argument("backup", type=Path)
        parser.add_argument("--replace", action="append", default=[], metavar="PATH",
                            help="An existing file to replace, as listed in the backup, "
                                 "e.g. app.db or db/coin-7.db. Repeatable.")

    def handle(self, *args, backup: Path, replace: list[str], **options):
        try:
            result = restore(backup, Path(settings.DATA_DIR), replace=replace)
        except (BackupError, OSError) as e:
            raise CommandError(f"nothing restored: {e}") from e
        for rel in result.restored:
            self.stdout.write(f"restored  {rel}")
        for rel, aside in result.replaced:
            self.stdout.write(f"replaced  {rel} (the old one is now {aside})")
        for rel in result.kept:
            self.stdout.write(f"kept      {rel} (exists; --replace {rel} to restore it)")
        campaigns = result.restored + [rel for rel, _ in result.replaced]
        campaigns = [rel for rel in campaigns if rel.startswith("db/")]
        if campaigns:
            self.stdout.write(self.style.WARNING(
                "These campaign DBs don't know what was sent after the backup was made: "
                + ", ".join(campaigns) + ". If a campaign was sending after that, don't "
                "send from it again before checking (docs/deploy.md, \"Restore\")."
            ))
