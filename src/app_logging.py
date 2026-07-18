"""Application logging and last-resort exception reporting.

The packaged app has no console, so every run writes a small rotating UTF-8 log
under the app data directory.  The helpers stay dependency-free so they can be
used during startup, before OCR, Tk, or the translation model is initialized.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading


LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "JapaneseHoverTranslator.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOGGER_NAME = "japanese_hover_translator"


def configure_logging(
    app_data_directory,
    *,
    logger_name=LOGGER_NAME,
    max_bytes=LOG_MAX_BYTES,
    backup_count=LOG_BACKUP_COUNT,
    console=None,
):
    """Return ``(logger, log_path)`` with bounded file and optional console logs.

    Repeated calls for the same logger are idempotent. Tests may pass a unique
    logger name and small ``max_bytes`` to exercise rotation without touching
    the application's logger.
    """
    log_directory = Path(app_data_directory) / LOG_DIRECTORY_NAME
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / LOG_FILE_NAME

    logger = logging.getLogger(logger_name)
    if getattr(logger, "_jht_configured_path", None) == str(log_path):
        return logger, log_path

    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console is None:
        console = not getattr(sys, "frozen", False) or os.environ.get(
            "JHT_DIAGNOSTIC_CONSOLE"
        ) == "1"
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    logger._jht_configured_path = str(log_path)
    return logger, log_path


def install_exception_logging(logger):
    """Log otherwise-uncaught main-thread and background-thread exceptions."""
    previous_sys_hook = sys.excepthook

    def sys_hook(exc_type, exc_value, traceback):
        """Replacement for sys.excepthook -- logs instead of (only) printing
        to stderr, which the packaged --windowed build has none of anyway."""
        if issubclass(exc_type, KeyboardInterrupt):
            previous_sys_hook(exc_type, exc_value, traceback)
            return
        logger.critical(
            "uncaught application exception",
            exc_info=(exc_type, exc_value, traceback),
        )

    sys.excepthook = sys_hook

    if hasattr(threading, "excepthook"):
        def thread_hook(args):
            """threading.excepthook replacement -- without this, an
            exception in a background thread (dwell worker, translation
            worker, hotkey listener) would print to stderr (invisible in a
            packaged --windowed build) and silently kill just that thread."""
            if args.exc_type is SystemExit:
                return
            logger.critical(
                "uncaught exception in thread %s",
                getattr(args.thread, "name", "unknown"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        threading.excepthook = thread_hook

