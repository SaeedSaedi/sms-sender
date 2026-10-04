import signal
import threading

from django.core.management.base import BaseCommand

from sms_sender_web.jobs.worker import Worker


class Command(BaseCommand):
    help = (
        "Run the dashboard's background jobs (sends, reconciliation, delivery "
        "and click updates), one at a time. Run exactly one worker."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Run at most one job, then exit.")
        parser.add_argument("--poll", type=float, default=2.0,
                            help="Seconds between looks for new jobs when idle.")

    def handle(self, *args, once: bool, poll: float, **options):
        stop = threading.Event()

        def on_signal(signum, _frame):
            # A running send stops gracefully: requests in flight finish and
            # are recorded, and the job is queued again for the next start.
            self.stdout.write(f"signal {signum}: stopping after the current request(s)")
            stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        worker = Worker(stop=stop)
        if once:
            job = worker.run_once()
            self.stdout.write(f"job: {job.pk} {job.state}" if job else "no job waiting")
        else:
            worker.run_forever(poll_sec=poll)
