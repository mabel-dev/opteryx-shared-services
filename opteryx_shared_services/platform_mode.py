"""The platform maintenance switch: one mode, read by every service.

The mode lives in the configuration document, ``(default)/config/<environment>``,
as a top-level ``platform_mode`` map beside ``values``::

    {
        "values": {...},                       # the configuration, as before
        "platform_mode": {
            "mode": "read_only",               # operational | read_only | reject | queue
            "reason": "catalog migration",     # optional, shown to callers
            "until": <timestamp>,              # optional, becomes Retry-After
            "set_by": "...", "set_at": <timestamp>,  # optional, for the audit trail
        },
    }

Beside ``values``, not inside it, on purpose. Inside, it would be an ordinary
configuration value: served by ``get_config`` from a cache up to five minutes
old, failing open, and copied into ``os.environ`` at import - where it would
become a ``PLATFORM_MODE`` override frozen at the value the process booted with.
As its own field it is read by this module alone, on this module's cadence and
failure rule, from the same document and over the same client.

What each mode allows is the table ``ALLOWED`` below, and nowhere else:

=========  ===========  ==========  ======  =======
service    operational  read_only   reject  queue
=========  ===========  ==========  ======  =======
jobs       all          reads       --      all (held: the worker claims nothing)
worker     all          reads       --      --
upload     all          --          --      --
flight     all          reads       --      --
odata      all          reads       --      --
control    all          --          --      --
xb500      all          reads       --      --
=========  ===========  ==========  ======  =======

xb500 is the scheduled housekeeping service: it lands logs, records billing,
compacts and expires datasets and reclaims storage, all directly on the
platform. Its scheduled runs are refused outside ``operational`` (or, in
``read_only``, anything that writes); Cloud Scheduler's next run does the work
once the mode allows it.

``queue`` holds work only where there is a queue to hold it in: jobs accepts the
submission and the worker leaves it pending. Flight, odata, upload and control
answer synchronously, so for them ``queue`` refuses exactly like ``reject`` - a
client left waiting on a connection until it times out is worse than a clear
refusal now.

What a service must NOT gate: the route that sets the mode, and its health and
status routes. ``control`` refuses everything outside ``operational``, so a gate
on the mode route would leave ``reject`` with no way back but a hand edit in
Firestore.

Failure rule - deliberately NOT the fail-open rule of ``config`` and
``billing.block``. A Firestore outage may be the very crisis the switch was set
for, so falling back to ``operational`` on a read error would turn everything
back on at the worst moment. Instead:

- a read error, or a ``platform_mode`` holding a mode this module does not know
  (a typo made in a hurry), keeps the last mode this process read successfully,
  and logs;
- only a process that has never read the document successfully assumes
  ``operational`` - a service must still start when the switch is unreadable;
- no ``platform_mode`` field (or no document) is a successful read of "no switch
  set": ``operational``. Deleting the field turns the switch off.

``PLATFORM_MODE`` in the process environment overrides the document. It is the
break-glass for when Firestore itself is unreachable: redeploy (or restart a
node) with it set. An unknown value there is ignored, and logged. A
``PLATFORM_MODE`` that the configuration client promoted out of ``values`` is
ignored too, for the freezing reason above - only a deployment's own variable
counts.

The document is read at most once per ``CACHE_TTL_SECONDS`` per process, so a
change reaches every instance within that window. A service that holds a
snapshot listener (the worker) can call ``set_mode_from_snapshot`` to apply a
change the moment it arrives.

Usage::

    from opteryx_shared_services import platform_mode

    refusal = platform_mode.check(platform_mode.FLIGHT, platform_mode.READ)
    if refusal is not None:
        raise fl.FlightUnavailableError(refusal.message)  # or a 503 with
        # headers={"Retry-After": str(refusal.retry_after_seconds)}

How a refusal is reported stays with each service; this module decides only
whether to refuse and what to say.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from typing import Callable
from typing import Optional

COLLECTION = "config"  # the configuration client's collection; see the docstring
FIELD = "platform_mode"
ENV_OVERRIDE = "PLATFORM_MODE"

CACHE_TTL_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 5.0
# Retry-After when the document gives no `until`, or an `until` already past.
DEFAULT_RETRY_AFTER_SECONDS = 60

OPERATIONAL = "operational"
READ_ONLY = "read_only"
REJECT = "reject"
QUEUE = "queue"
MODES = (OPERATIONAL, READ_ONLY, REJECT, QUEUE)

READ = "read"
WRITE = "write"
OPERATIONS = (READ, WRITE)

JOBS = "jobs"
WORKER = "worker"
UPLOAD = "upload"
FLIGHT = "flight"
ODATA = "odata"
CONTROL = "control"
XB500 = "xb500"
SERVICES = (JOBS, WORKER, UPLOAD, FLIGHT, ODATA, CONTROL, XB500)

_ALL = frozenset(OPERATIONS)
_READS = frozenset({READ})
_NONE: frozenset = frozenset()

# service -> mode -> the operations allowed. The one statement of the matrix.
ALLOWED: dict[str, dict[str, frozenset]] = {
    JOBS: {OPERATIONAL: _ALL, READ_ONLY: _READS, REJECT: _NONE, QUEUE: _ALL},
    WORKER: {OPERATIONAL: _ALL, READ_ONLY: _READS, REJECT: _NONE, QUEUE: _NONE},
    UPLOAD: {OPERATIONAL: _ALL, READ_ONLY: _NONE, REJECT: _NONE, QUEUE: _NONE},
    FLIGHT: {OPERATIONAL: _ALL, READ_ONLY: _READS, REJECT: _NONE, QUEUE: _NONE},
    ODATA: {OPERATIONAL: _ALL, READ_ONLY: _READS, REJECT: _NONE, QUEUE: _NONE},
    CONTROL: {OPERATIONAL: _ALL, READ_ONLY: _NONE, REJECT: _NONE, QUEUE: _NONE},
    XB500: {OPERATIONAL: _ALL, READ_ONLY: _READS, REJECT: _NONE, QUEUE: _NONE},
}

MESSAGES = {
    READ_ONLY: (
        "The platform is in read-only maintenance. Changes, uploads and account "
        "management are paused; queries still run."
    ),
    REJECT: "The platform is paused for maintenance. Please try again later.",
    QUEUE: (
        "The platform is paused for maintenance. Submitted jobs are held and will run "
        "when it resumes; interactive connections are refused until then."
    ),
}

logger = logging.getLogger("opteryx.platform_mode")


@dataclass(frozen=True)
class State:
    """The mode in force, and what the document said about it."""

    mode: str = OPERATIONAL
    reason: Optional[str] = None
    until: Optional[datetime.datetime] = None
    source: str = "default"  # "document", "env", or "default" (never read successfully)


@dataclass(frozen=True)
class Refusal:
    """Why an operation is refused, ready for a 503 / FlightUnavailableError."""

    mode: str
    message: str
    retry_after_seconds: int
    reason: Optional[str] = None


def allowed(mode: str, service: str, operation: str) -> bool:
    """The matrix alone: may ``service`` perform ``operation`` in ``mode``?

    Raises ``ValueError`` for a name not in the matrix - that is a bug in the
    caller, and guessing would silently put it on one side of the switch.
    """
    if service not in ALLOWED:
        raise ValueError(f"unknown service {service!r}; expected one of {SERVICES}")
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}; expected one of {OPERATIONS}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    return operation in ALLOWED[service][mode]


def _as_datetime(value: Any) -> Optional[datetime.datetime]:
    """Firestore Timestamp / datetime / ISO string -> aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _retry_after(state: State, now: datetime.datetime) -> int:
    if state.until is not None:
        remaining = int((state.until - now).total_seconds())
        if remaining > 0:
            return remaining
    return DEFAULT_RETRY_AFTER_SECONDS


def refusal_for(
    state: State, service: str, operation: str, now: Optional[datetime.datetime] = None
) -> Optional[Refusal]:
    """The refusal for ``operation`` on ``service`` under ``state``, or None if allowed."""
    if allowed(state.mode, service, operation):
        return None
    now = now or datetime.datetime.now(tz=datetime.timezone.utc)
    message = MESSAGES[state.mode]
    if state.reason:
        message = f"{message} ({state.reason})"
    return Refusal(
        mode=state.mode,
        message=message,
        retry_after_seconds=_retry_after(state, now),
        reason=state.reason,
    )


def _default_environment() -> Optional[str]:
    from opteryx_shared_services import config

    return config._default_client.environment


def _promoted_by_config(key: str, value: str) -> bool:
    """Did the configuration client put this value into ``os.environ`` itself?"""
    from opteryx_shared_services import config

    return config._default_client._promoted_by_us(key, value)


def _default_firestore() -> Any:
    # The configuration client's own channel to `(default)`: no second client.
    from opteryx_shared_services import config

    return config._default_client._client()


class PlatformMode:
    """Reads and caches the platform_mode field of the config document. See the module docstring for the rules."""

    def __init__(
        self,
        environment: Optional[str] = None,
        client_provider: Optional[Callable[[], Any]] = None,
        cache_ttl: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        promoted: Callable[[str, str], bool] = _promoted_by_config,
    ):
        self._environment = environment
        self._client_provider = client_provider
        self._promoted = promoted
        self._cache_ttl = cache_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._state = State()
        self._fetched_at: Optional[float] = None
        self._bad_override: Optional[str] = None

    def _env_override(self) -> Optional[State]:
        value = os.environ.get(ENV_OVERRIDE)
        if not value:
            return None
        mode = value.strip().lower()
        problem = None
        if self._promoted(ENV_OVERRIDE, value):
            problem = "it was promoted from the config document's values, not set by the deployment"
        elif mode not in MODES:
            problem = f"not one of {MODES}"
        if problem is not None:
            if value != self._bad_override:  # once per value, not once per request
                self._bad_override = value
                logger.error("ignoring %s=%r: %s", ENV_OVERRIDE, value, problem)
            return None
        return State(mode=mode, source="env")

    def _fetch(self) -> None:
        """Refresh from Firestore. On any failure keep the last state, never reset it."""
        environment = self._environment or _default_environment()
        if not environment:
            logger.warning(
                "platform mode not read: no environment detected (keeping %s)", self._state.mode
            )
            return
        try:
            provider = self._client_provider or _default_firestore
            snapshot = (
                provider()
                .collection(COLLECTION)
                .document(environment)
                .get(timeout=READ_TIMEOUT_SECONDS)
            )
        except Exception as exc:  # noqa: BLE001 - see the failure rule
            logger.warning(
                "platform mode read failed, keeping %s: %s: %s",
                self._state.mode,
                type(exc).__name__,
                exc,
            )
            return
        self.set_mode_from_snapshot(snapshot)

    def set_mode_from_snapshot(self, snapshot: Any) -> State:
        """Apply a config-document snapshot (from a read or a listener). Returns the state in force."""
        data = (snapshot.to_dict() or {}) if getattr(snapshot, "exists", False) else {}
        data = data.get(FIELD)
        if data is None:
            new = State(source="document")
        else:
            data = data if isinstance(data, dict) else {}
            mode = data.get("mode")
            if mode not in MODES:
                logger.error(
                    "config platform_mode holds unknown mode %r, keeping %s",
                    mode,
                    self._state.mode,
                )
                return self._state
            reason = data.get("reason")
            new = State(
                mode=mode,
                reason=reason if isinstance(reason, str) and reason else None,
                until=_as_datetime(data.get("until")),
                source="document",
            )
        if new.mode != self._state.mode:
            logger.warning(
                "platform mode is now %s (was %s)%s",
                new.mode,
                self._state.mode,
                f": {new.reason}" if new.reason else "",
            )
        self._state = new
        return new

    def current(self) -> State:
        """The state in force now, reading the document if the cache has expired."""
        override = self._env_override()
        if override is not None:
            return override
        with self._lock:
            now = self._clock()
            if self._fetched_at is None or now - self._fetched_at >= self._cache_ttl:
                # Stamped on every attempt, failures included, so an outage is
                # retried once per TTL rather than on every request.
                self._fetched_at = now
                self._fetch()
            return self._state

    def check(self, service: str, operation: str) -> Optional[Refusal]:
        """Why ``operation`` on ``service`` must be refused now, or None."""
        return refusal_for(self.current(), service, operation)

    def refresh(self) -> None:
        """Forget the cache so the next lookup reads the document."""
        with self._lock:
            self._fetched_at = None


_default = PlatformMode()


def current() -> State:
    return _default.current()


def check(service: str, operation: str) -> Optional[Refusal]:
    return _default.check(service, operation)


def set_client_provider(provider: Callable[[], Any]) -> None:
    """Read with a client the service already holds instead of the config client's."""
    _default._client_provider = provider


def set_mode_from_snapshot(snapshot: Any) -> State:
    return _default.set_mode_from_snapshot(snapshot)


def refresh() -> None:
    _default.refresh()


__all__ = [
    "ALLOWED",
    "CONTROL",
    "FLIGHT",
    "JOBS",
    "MESSAGES",
    "MODES",
    "ODATA",
    "OPERATIONAL",
    "PlatformMode",
    "QUEUE",
    "READ",
    "READ_ONLY",
    "REJECT",
    "Refusal",
    "SERVICES",
    "State",
    "UPLOAD",
    "WORKER",
    "WRITE",
    "XB500",
    "allowed",
    "check",
    "current",
    "refresh",
    "refusal_for",
    "set_client_provider",
    "set_mode_from_snapshot",
]
