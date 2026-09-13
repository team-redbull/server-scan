"""Structured logging via structlog.

structlog is the 2026-standard choice for async Python services over a
hand-rolled `logging.Formatter`: it gives bound loggers that carry request
context (request_id, server_id, ...) across a call chain without threading
it through every function signature, and a processor pipeline that renders
JSON in production and a readable console format in development from the
same log calls.

FastAPI, Uvicorn, and PyMongo all log through the stdlib `logging` module.
So that *all* logs — ours and theirs — end up as consistent JSON lines
sharing one schema, stdlib logging is routed through structlog's
`ProcessorFormatter` rather than run as a second, separately configured
pipeline (the "dual pipeline" mistake that produces two different log shapes
in the same service).

Never logged, even if passed as a field: raw request bodies, Authorization
headers, tokens, or credentials. `_drop_sensitive_keys` is the last line of
defense if a value with one of those names is ever passed by mistake.
"""

from __future__ import annotations

import logging
import logging.config
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {"password", "token", "authorization", "secret", "api_key", "credential"}
)


def _scrub(value: Any) -> Any:
    """
    Recursively drop `_SENSITIVE_KEYS` from a value, at any nesting depth.

    Lists are walked too — docs/architecture.md, "Observability", for the
    shapes a top-level-only check missed.

    Args:
        value (Any): The value to scrub — a dict, a list, or anything
            else (returned unchanged).

    Returns:
        Any: `value` with every `_SENSITIVE_KEYS` key removed from any
            dict found at any depth.
    """
    if isinstance(value, MutableMapping):
        return {k: _scrub(v) for k, v in value.items() if k.lower() not in _SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _drop_sensitive_keys(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            del event_dict[key]
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(*, level: str, service_name: str, environment: str) -> None:
    """
    Configure structlog + stdlib logging to share one rendering pipeline.

    Renders JSON in production and a readable console format otherwise.
    Call once at startup, before any logger is used.

    Args:
        level (str): The root logger level, e.g. "INFO".
        service_name (str): Stamped onto every log line as `service`.
        environment (str): Stamped onto every log line as `environment`;
            `"production"` selects the JSON renderer.
    """
    is_production = environment == "production"

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _drop_sensitive_keys,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if is_production else structlog.dev.ConsoleRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            *shared_processors,
            lambda _l, _m, ed: {**ed, "service": service_name, "environment": environment},
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)

    # Redundant with `RequestContextMiddleware`'s one line per request.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
