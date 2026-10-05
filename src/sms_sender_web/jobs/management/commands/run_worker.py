import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from sms_sender.sharing import FRESH_SEC
from sms_sender_web.jobs.worker import Worker, other_worker

# Another worker's last sign of life ages out within this long: one that
# stopped without signing off (killed, or an older version) is waited for.
WAIT_SEC = FRESH_SEC + 10
LOOK_EVERY_SEC = 5.0


class Command(BaseCommand):
    help = (
        "Run the dashboard's background jobs (sends, reconciliation, delivery "
        "and click updates), one at a time. Run exactly one worker: this one "
        "waits while another is alive on the same data folder, and exits 2 if "
        "it stays."
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
        self._wait_for_others(worker, stop)
        if stop.is_set():
            return
        if once:
            job = worker.run_once()
            self.stdout.write(f"job: {job.pk} {job.state}" if job else "no job waiting")
        else:
            worker.run_forever(poll_sec=poll)

    def _wait_for_others(self, worker: Worker, stop: threading.Event) -> None:
        other = other_worker(worker.id)
        waited = 0.0
        if other:
            self.stdout.write(f"waiting: {other}; it has to stop first (up to {WAIT_SEC} s)")
        while other and waited < WAIT_SEC and not stop.is_set():
            stop.wait(LOOK_EVERY_SEC)
            waited += LOOK_EVERY_SEC
            other = other_worker(worker.id)
        if other and not stop.is_set():
            raise CommandError(f"{other}. Run exactly one worker per data folder.", returncode=2)
