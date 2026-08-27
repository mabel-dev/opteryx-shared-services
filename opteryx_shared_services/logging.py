"""Structured logging for Opteryx services, and the audit wire format.

This is the canon assembled from eight divergent per-service implementations.
Two of them were right in different ways, five were emitting audit records that
the fleet's own ingestion could never see, and the differences were invisible
because nothing compared them. What follows is the union, with the reasoning
for each part, because the parts are not interchangeable.

THE AUDIT WIRE FORMAT IS A CROSS-SERVICE CONTRACT
-------------------------------------------------
`xb500.opteryx`'s `transform_audit_logs` ingests Cloud Logging entries whose
``jsonPayload.severity`` is ``"AUDIT"`` into ``ops.audit_log``;
`transform_billing_logs` selects ``"BILLING"`` the same way. That imposes three
requirements on every service that calls ``logger.audit(...)``:

1. The audit line must be **valid JSON on its own**. Cloud Logging only parses
   a stdout line into ``jsonPayload`` when the whole line is JSON -- a
   ``"time | LEVEL | name | {...}"`` prefix makes it opaque ``textPayload``.
2. The payload must carry ``severity`` **itself**. The LogEntry's own
   GCP-assigned severity is not what the transform selects on.
3. A caller-supplied severity must **survive verbatim**. Billing events pass
   ``severity="BILLING"`` and are ingested by a different transform.

Cloud Logging's recognised severities are DEFAULT/DEBUG/INFO/NOTICE/WARNING/
ERROR/CRITICAL/ALERT/EMERGENCY, but an *unrecognised* string in the ``severity``
key survives into ``jsonPayload.severity`` rather than being dropped -- which is
what makes ``"AUDIT"`` and ``"BILLING"`` work at all.

So audit records get their own handler, on stdout, with no formatter prefix,
and their payload is passed through untouched. Ordinary log records go to a
separate handler that never sees them.

WHY ORDINARY RECORDS ARE ALSO JSON ON CLOUD RUN
-----------------------------------------------
Formatted text lands as ``textPayload`` at DEFAULT severity, so severity-based
filtering and alerting cannot see even an ERROR. Under `K_SERVICE` (every Cloud
Run deployment) ordinary records are therefore rendered as JSON with a mapped
``severity``; everywhere else they stay human-readable, because
``timestamp | LEVEL | name | message`` beats a JSON blob in a terminal.
``LOG_FORMAT=json`` / ``LOG_FORMAT=text`` overrides the choice either way.

WHAT THIS REPLACES
------------------
The removed `orso.logging` module, which returned a `GoogleLogger` under
`K_SERVICE` and a stdlib logger otherwise. That class had a different method
surface from `logging.Logger` (no ``.exception()``, single argument only),
which caused deploy-only crashes *inside exception handlers*. A stdlib logger
with a formatter swapped underneath it gets the same structured output without
a second, subtly different logger class.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any
from typing import Iterable
from typing import Optional

__all__ = [
    "ALERT",
    "AUDIT",
    "AuditFormatter",
    "StructuredFormatter",
    "get_logger",
    "set_log_name",
    "set_trace_context",
]

# Both sit above ERROR so audit and alert records are emitted whatever level the
# service is configured at. The numbers mirror the ones `orso.logging` used, so
# call sites and any stored level filters carry over unchanged.
AUDIT = 80
ALERT = 90

logging.addLevelName(AUDIT, "AUDIT")
logging.addLevelName(ALERT, "ALERT")

# Python level -> Cloud Logging LogSeverity, for ORDINARY records only. AUDIT and
# ALERT are deliberately absent: those carry their own severity string inside the
# payload, and mapping them here is exactly the bug that made worker.opteryx's
# audit records emit as "NOTICE" and vanish from `ops.audit_log`.
_SEVERITY_BY_LEVEL = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

_DEFAULT_LOG_NAME = "opteryx"

# Libraries whose warnings are part of operating a service, not noise from a
# dependency:
#
#   opteryx.config_client  - the configuration document could not be read.
#   opteryx_catalog        - "github sink selected but no repo configured",
#                            "sink GitHubSink failed".
#
# Without a handler these inherit the root logger, which nothing configures, so
# WARNING survives only via Python's last-resort handler -- unformatted and easy
# to miss. Both are the kind of failure that otherwise presents as a channel
# simply being quiet.
#
# WARNING, not the service's own level: `config_client` logs a line per key the
# first time each resolves, which is a dozen lines of "resolved from default" on
# every instance start and says nothing once a service is working.
_ADOPTED_LOGGERS = ("opteryx.config_client", "opteryx_catalog")
_ADOPTED_LEVEL = logging.WARNING

# Set per request by the audit middleware from the inbound
# `X-Cloud-Trace-Context` header, so every line logged while serving a request
# correlates to that request in the Cloud Logging UI.
_trace_context: ContextVar[Optional[str]] = ContextVar("trace_context", default=None)

_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def set_trace_context(trace: Optional[str]) -> None:
    """Record the current request's trace id for subsequent log lines."""
    _trace_context.set(trace)


def set_log_name(name: str) -> None:
    """Set the logger name `get_logger()` returns when called without one.

    Call it once, at import of the service's own logging shim, before anything
    has logged. Changing it later leaves records already emitted under the old
    name, and the previously configured logger keeps its handlers.
    """
    global _DEFAULT_LOG_NAME
    _DEFAULT_LOG_NAME = name


def _payload_logger(level: int, default_severity: str):
    """Build a `Logger.audit` / `Logger.alert` method for `level`.

    A dict message is serialised here, not in the formatter, with
    `default_severity` stamped in -- and the caller's own `severity` winning,
    because a billing event passes `severity="BILLING"` and must keep it.
    """

    def emit(self: logging.Logger, message: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(message, dict):
            message = json.dumps({"severity": default_severity, **message}, default=str)
        elif isinstance(message, bytes):
            message = message.decode()
        if self.isEnabledFor(level):
            # stacklevel=2 so `%(pathname)s:%(lineno)d` names the call site
            # rather than this module.
            self._log(level, message, args, stacklevel=2, **kwargs)

    return emit


# `if not hasattr`, because these patch the shared `logging.Logger` class: a
# second import must not rebind a method a service has already replaced, and
# `logging.getLoggerClass()` may not be `Logger` if something else got there
# first.
for _name, _level in (("audit", AUDIT), ("alert", ALERT)):
    if not hasattr(logging.getLoggerClass(), _name):
        setattr(logging.getLoggerClass(), _name, _payload_logger(_level, _name.upper()))


class _AuditOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno in (AUDIT, ALERT)


class _ExcludeAuditFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno not in (AUDIT, ALERT)


_RESERVED_RECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class StructuredFormatter(logging.Formatter):
    """Render an ordinary record as a single-line JSON object for Cloud Logging.

    One line per record, no prefix: Cloud Logging only parses a stdout/stderr
    line into `jsonPayload` when the whole line is valid JSON.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": _SEVERITY_BY_LEVEL.get(
                record.levelno, logging.getLevelName(record.levelno)
            ),
            "message": record.getMessage(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": str(record.lineno),
                "function": record.funcName,
            },
            "logger": record.name,
        }

        trace = _trace_context.get()
        if trace:
            # Cloud Run's own trace id, so app lines group under the request
            # entry rather than floating loose in the log stream.
            payload["logging.googleapis.com/trace"] = trace

        if record.exc_info:
            # Error Reporting picks up an exception when the traceback is part
            # of `message`, not a sibling field -- so `logger.exception(...)`
            # produces a grouped, alertable error rather than a bare string.
            payload["message"] = f"{payload['message']}\n{self.formatException(record.exc_info)}"

        # Anything passed via `extra=` that is not a standard LogRecord
        # attribute rides along as its own jsonPayload field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS and not key.startswith("_"):
                payload.setdefault(key, value)

        return json.dumps(payload, default=str)


class AuditFormatter(logging.Formatter):
    """Emit an audit or alert record's own JSON verbatim.

    `logger.audit(dict)` has already serialised the payload, including its
    `severity` discriminator. Re-wrapping it here would nest the fields one
    level deeper and break every downstream selector, so the message is passed
    straight through -- in BOTH output modes, since audit records are machine
    -read in local development too.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        trace = _trace_context.get()
        if not trace:
            return message
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return message
        if not isinstance(payload, dict):
            return message
        payload.setdefault("logging.googleapis.com/trace", trace)
        return json.dumps(payload, default=str)


def _use_structured_logging() -> bool:
    """Structured output on Cloud Run, or wherever it is asked for explicitly."""
    explicit = os.environ.get("LOG_FORMAT", "").strip().lower()
    if explicit:
        return explicit == "json"
    return bool(os.environ.get("K_SERVICE"))


def _adopt(handler: logging.Handler, names: Iterable[str]) -> None:
    """Give third-party loggers this service's handler, under their own names.

    Same handler, same format, their own `record.name` -- so a line's origin
    stays readable and a library warning cannot go out unformatted through
    Python's last-resort handler.
    """
    for name in names:
        adopted = logging.getLogger(name)
        adopted.setLevel(_ADOPTED_LEVEL)
        if not adopted.handlers:
            adopted.addHandler(handler)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return the service's configured logger.

    Configured once per name: the handlers are attached only if the logger has
    none, and the level is only set then, because callers adjust the level after
    the first call and expect it to stick.
    """
    logger = logging.getLogger(name or _DEFAULT_LOG_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(int(os.environ.get("LOGGING_LEVEL", logging.INFO)))
    structured = _use_structured_logging()

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter() if structured else logging.Formatter(_TEXT_FORMAT))
    handler.addFilter(_ExcludeAuditFilter())
    logger.addHandler(handler)

    # Always stdout, always raw JSON, in both output modes -- this is the wire
    # format xb500's ingestion selects on, and it is not a display choice.
    audit_handler = logging.StreamHandler(sys.stdout)
    audit_handler.setFormatter(AuditFormatter())
    audit_handler.addFilter(_AuditOnlyFilter())
    logger.addHandler(audit_handler)

    # Records are emitted by this logger's own handlers; letting them also
    # propagate to the root logger would duplicate every line under uvicorn's
    # default configuration.
    logger.propagate = False

    _adopt(handler, _ADOPTED_LOGGERS)
    return logger
