"""Structured logging.

Every log line carries whatever context has been bound for the current task,
which in practice means ``event_id``, ``user_id``, ``member_id`` and
``request_id``. This is the difference between a ten-minute incident and a
three-hour one during a live event.
"""

import logging
import sys
from typing import Any

import structlog

from app.infrastructure.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    renderer: structlog.types.Processor
    if settings.is_production_like:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.debug else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
