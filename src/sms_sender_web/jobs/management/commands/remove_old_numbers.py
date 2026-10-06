"""What the worker does once a day (plan 06, D2): remove phone numbers
older than the system setting, counts kept. `--dry-run` only says what
would go."""
from django.core.management.base import BaseCommand

from sms_sender_web import retention


class Command(BaseCommand):
    help = "Remove phone numbers older than they're kept (System settings), counts kept."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only say what would be removed.")

    def handle(self, *args, dry_run: bool = False, **options):
        removed = (retention.remove_old_numbers(dry_run=True) if dry_run else retention.run_and_record())
        verb = "would be removed" if dry_run else "removed"
        self.stdout.write(f"campaigns  {len(removed.campaigns)} {verb}: {', '.join(removed.campaigns) or '-'}")
        if not dry_run:
            self.stdout.write(f"numbers    {removed.numbers}")
        self.stdout.write(f"segments   {len(removed.segments)}: {', '.join(removed.segments) or '-'}")
        self.stdout.write(f"downloads  {removed.exports}")
        self.stdout.write(f"backups    {len(removed.backups)}: {', '.join(removed.backups) or '-'}")
        self.stdout.write(f"records    {removed.records} masked")
        if removed.kept:
            self.stdout.write(f"kept       {', '.join(removed.kept)} (a job is on its way: next time)")
