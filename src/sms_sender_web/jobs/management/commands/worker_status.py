import socket

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sms_sender_web.jobs.models import WorkerBeat
from sms_sender_web.jobs.worker import ALIVE_WITHIN


class Command(BaseCommand):
    help = (
        "Exit 0 if the worker on this host was alive within the last minute, "
        "else 1: the worker container's health check. --any: any worker."
    )

    def add_arguments(self, parser):
        parser.add_argument("--any", action="store_true", dest="any_host",
                            help="Any worker, on any host.")

    def handle(self, *args, any_host: bool, **options):
        beats = WorkerBeat.objects.filter(seen_at__gte=timezone.now() - ALIVE_WITHIN)
        if not any_host:
            # Worker IDs are "<hostname>:<pid>".
            beats = beats.filter(worker_id__startswith=f"{socket.gethostname()}:")
        beat = beats.order_by("-seen_at").first()
        if beat is None:
            raise CommandError("no worker alive within the last minute")
        self.stdout.write(f"alive: {beat}")
