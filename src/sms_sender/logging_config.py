"""Structured logging: console + rotating file. key=value formatter for grep-ability."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class KeyValueFormatter(logging.Formatter):
    """Renders `extra={...}` fields as key=value pairs after the message."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"ts={self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"level={record.levelname} "
            f"logger={record.name} "
            f"msg={record.getMessage()!r}"
        )
        extras = []
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            extras.append(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}")
        if extras:
            base += " " + " ".join(extras)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup(
    *,
    log_file: str | Path | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Wipe any prior handlers so re-runs (and tests) don't double-log, and
    # close them, so a log file isn't left open behind.
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    fmt = KeyValueFormatter()

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Quiet down chatty libraries.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
