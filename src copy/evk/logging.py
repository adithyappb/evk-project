"""Loguru-based structured logging. Call `configure_logging()` once at process start."""

from __future__ import annotations

import logging
import sys

from loguru import logger

from evk.config import get_settings

_CONFIGURED = False


class _InterceptHandler(logging.Handler):
    """Redirects stdlib logging (used by uvicorn, google libs) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin shim
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging() -> None:
    """Idempotently configure loguru + intercept stdlib logging."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.app_log_level.upper(),
        enqueue=False,
        backtrace=False,
        diagnose=settings.app_env == "dev",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "| {extra} - <level>{message}</level>"
        ),
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logging.getLogger(noisy).handlers = [_InterceptHandler()]
        logging.getLogger(noisy).propagate = False
    _CONFIGURED = True


__all__ = ["configure_logging", "logger"]
