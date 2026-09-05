#!/usr/bin/env python3
"""
ENZO - Structured Logging System (Rotating Log Files & Formatted Console)
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from enzo.core.config import LOGS_DIR

# Where console logs go. The engine keeps stdout; the control plane (enzoctl)
# switches this to stderr so that `enzoctl --json <cmd> | jq` receives JSON and
# nothing else on stdout. Loggers are created lazily all over the codebase, so a
# one-shot "move the existing handlers" pass is not enough - the stream has to be
# read here, at creation time, as well.
_CONSOLE_STREAM = sys.stdout


def set_console_stream(stream) -> int:
    """Route console logging to `stream` (and move handlers already created)."""
    global _CONSOLE_STREAM
    _CONSOLE_STREAM = stream
    moved = 0
    try:
        loggers = [logging.getLogger()]
        loggers += [logging.getLogger(n) for n in list(logging.root.manager.loggerDict)]
        for lg in loggers:
            for h in list(getattr(lg, "handlers", [])):
                if (isinstance(h, logging.StreamHandler)
                        and not isinstance(h, RotatingFileHandler)
                        and getattr(h, "stream", None) not in (None, stream)):
                    try:
                        h.setStream(stream)
                    except Exception:                                # noqa: BLE001
                        h.stream = stream
                    moved += 1
    except Exception:                                                # noqa: BLE001
        pass
    return moved


def setup_logger(name: str = "enzo", log_to_console: bool = True, log_to_file: bool = True, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_enzo_configured", False):
        return logger

    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    if log_to_console and not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler(_CONSOLE_STREAM)
        ch.setLevel(level)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    if log_to_file and not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_file = os.path.join(LOGS_DIR, "enzo.log")
        fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    logger._enzo_configured = True
    return logger


def get_logger(name: str = "enzo") -> logging.Logger:
    # Each module gets its OWN named logger (previously all callers shared a
    # single global logger, so [%(name)s] in log lines was always the first
    # module that happened to initialize).
    return setup_logger(name)
