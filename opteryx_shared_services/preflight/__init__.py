"""Prove a service's dependencies BEFORE the server process exists.

    python -m app.preflight && exec python -m uvicorn app.main:service ...

Run from the Dockerfile's CMD, ahead of the ASGI server, so a service that
cannot reach Firestore, GCS or Secret Manager exits the container non-zero
without ever opening a port. On Cloud Run that fails the revision: the deploy
does not complete and traffic stays on the revision that was already serving. A
broken build cannot swap out a working one.

That guarantee has to live out here, in its own process, and NOT only in the
ASGI lifespan. A container running `uvicorn --workers ${WEB_CONCURRENCY}` has a
supervisor that binds the listening socket in the PARENT and respawns any child
that dies (`Multiprocess.keep_subprocess_alive`). A lifespan that raises
therefore kills one child, the parent immediately starts another, and the port
stays open throughout -- so the revision looks alive while no worker can serve a
thing. The lifespan check remains as a second line of defence (it covers a
worker respawned long after startup, and running uvicorn by hand), but it is not
what makes the container fail.

The same probes back `/health`, so a dependency that breaks *after* startup (a
rotated service account, a deleted bucket, a revoked grant) is visible without
waiting for a request to fail.

USAGE
-----
Each service keeps a small `app/preflight.py` that declares what it needs and
nothing else::

    import sys

    from opteryx_shared_services.preflight import Preflight
    from opteryx_shared_services.preflight import firestore_read
    from opteryx_shared_services.preflight import gcs_list
    from opteryx_shared_services.preflight import gcs_write
    from opteryx_shared_services.preflight import secret_manager

    preflight = Preflight(
        component="worker",
        checks=(
            secret_manager(),
            firestore_read("firestore-jobs", collection="jobs"),
            firestore_read(
                "firestore-catalog",
                collection="$catalog",
                database_key="FIRESTORE_DATABASE",
                covered_by="firestore-jobs",
            ),
            gcs_list("gcs-data", bucket_key="GCS_BUCKET"),
            gcs_write("gcs-results", bucket_key="RESULTS_BUCKET"),
        ),
    )

    if __name__ == "__main__":
        sys.exit(preflight.main())

`app/main.py`'s lifespan calls `preflight.run_startup_checks()`; the health
route calls `preflight.readiness()`. `component` names the service in every
alert -- it prefixes the ticket title, becomes a label, and SALTS THE
FINGERPRINT, so two services reporting into the same repo stay distinguishable.

Probe builders take CONFIGURATION KEYS, not values. The bucket and database
names are resolved when the probe runs, not when the module is imported --
configuration is promoted into `os.environ` during startup, so a name captured
at import can be older than the one the rest of the process is using.

WHY THESE PROBE SHAPES
----------------------
Each probe exercises the permission the service actually uses, not a proxy for
it. That distinction matters more than it sounds: a bucket that is read is
probed with a LIST; a bucket that is only ever written is probed with a CREATE.
A service account holding `objectCreator` on a results bucket (which is enough
to serve every job) would fail a read probe, and one holding only
`objectViewer` would sail through a read probe and then fail every write at the
first upload. Declare the direction the service uses; do not declare both
because it is safer.

Results are cached for `CACHE_TTL_SECONDS`. `/health` is typically
unauthenticated, and these probes make real RPCs (some of them writes), so
without the cache the endpoint would be a way to bill the project by curl loop.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple

from opteryx_shared_services.config import config_status
from opteryx_shared_services.config import get_config
from opteryx_shared_services.logging import get_logger

logger = get_logger()

__all__ = [
    "CheckResult",
    "DependencyUnavailable",
    "PROBE_TIMEOUT_SECONDS",
    "Preflight",
    "custom",
    "firestore_read",
    "gcp_project",
    "gcs_list",
    "gcs_write",
    "get_config",
    "secret_manager",
]

# Bounds a single probe's RPC. Generous enough for a cold gRPC channel plus a
# credential refresh, short enough that several of them cannot hold a health
# request (or container startup) open for a minute.
PROBE_TIMEOUT_SECONDS = 5.0

# ...and bounds the whole set, since a client that hangs *before* it makes an
# RPC (ADC discovery, metadata server) never reaches the per-call timeout.
OVERALL_TIMEOUT_SECONDS = 15.0

CACHE_TTL_SECONDS = 30.0

# Firestore document read by the probes. It is not expected to exist -- a get on
# a missing document is a successful read, and requires exactly the permission
# the service needs. It costs one document read, which is what makes it cheap
# enough to run on a health endpoint.
#
# NOT `__healthcheck__`: Firestore reserves every id matching `__.*__` and
# rejects a read of one with InvalidArgument, so the probe would have failed
# whatever the caller's permissions were.
PROBE_DOCUMENT = "_healthcheck_probe"

# Where a bucket write probe puts its object. Deleted immediately afterwards;
# the prefix exists so a bucket lifecycle rule can mop up if the service account
# cannot delete what it creates (see `gcs_write`).
PROBE_PREFIX = "_healthcheck/"

DEFAULT_DATABASE = "(default)"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    duration_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 1),
        }


class DependencyUnavailable(RuntimeError):
    """A dependency the service needs could not be reached."""


# --- shared clients ---------------------------------------------------------


def project_id() -> Optional[str]:
    """The GCP project from the environment, or None.

    Read on every call rather than bound to a module constant: configuration is
    promoted into `os.environ` during startup, so a value captured at import can
    be older than the one the rest of the process is using.
    """
    return (
        os.environ.get("GCP_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )


def gcp_project() -> Optional[str]:
    """The GCP project the probes report against.

    Falls back to whatever ADC resolved, because Cloud Run sets none of
    `GCP_PROJECT_ID` / `GCP_PROJECT` / `GOOGLE_CLOUD_PROJECT` by default -- the
    google-cloud clients get theirs from the metadata server via ADC, and asking
    the auth layer is cheaper than constructing a service client just to read
    the project off it.

    Public because a `custom` probe usually needs it -- a Cloud Tasks queue path
    or a Pub/Sub topic is built from the project, and leaving services to work
    that out individually is how eight different answers happen.
    """
    explicit = project_id()
    if explicit:
        return explicit
    try:
        import google.auth

        return google.auth.default()[1]
    except Exception:
        return None


# Every client is imported and built lazily, inside the probe that needs it, so
# a service declaring only Firestore checks never imports google-cloud-storage.


@lru_cache(maxsize=4)
def _firestore_client_for(database: Optional[str]):
    from google.cloud import firestore

    project = project_id()
    kwargs: Dict[str, Any] = {}
    if project:
        kwargs["project"] = project
    if database and database != DEFAULT_DATABASE:
        kwargs["database"] = database
    return firestore.Client(**kwargs)


@lru_cache(maxsize=1)
def _storage_client():
    from google.cloud import storage

    project = project_id()
    return storage.Client(project=project) if project else storage.Client()


@lru_cache(maxsize=1)
def _secret_manager_client():
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()


# --- probe builders ---------------------------------------------------------


def _probe(name: str, body: Callable[[], CheckResult]) -> Callable[[], CheckResult]:
    """Tag a probe callable with the name the engine and alerts report it under."""
    body.check_name = name  # type: ignore[attr-defined]
    return body


def secret_manager(name: str = "secret-manager", secret_key: str = "HEALTH_CHECK_SECRET"):
    """Secret Manager is reachable, with credentials this project accepts.

    Two shapes, because most services' use is narrow -- one or two named
    secrets read with `access_secret_version`, and nothing else.

    - the configuration names a secret (`HEALTH_CHECK_SECRET` by default): read
      its latest version. This is the real thing -- the exact call, on a secret
      the deployment says it needs -- and a denial here is a genuine failure.
    - otherwise: list one secret, which only proves the API is enabled and the
      credentials are good. `PermissionDenied` on the LIST is reported as
      healthy-with-a-note, deliberately: `roles/secretmanager.secretAccessor`
      granted on individual secrets (the least-privilege grant, and the right
      one) carries no `secrets.list`, so failing here would take down a
      correctly-configured service for lacking a permission it never uses.
    """

    def probe() -> CheckResult:
        from google.api_core import exceptions as api_exceptions

        project = gcp_project()
        if not project:
            return CheckResult(name, False, "no GCP project could be determined")

        client = _secret_manager_client()
        secret_name = get_config(secret_key)

        if secret_name:
            path = f"projects/{project}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(
                request={"name": path}, timeout=PROBE_TIMEOUT_SECONDS
            )
            size = len(response.payload.data)
            return CheckResult(name, True, f"read {secret_name} ({size} bytes)")

        try:
            pager = client.list_secrets(
                request={"parent": f"projects/{project}", "page_size": 1},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            next(iter(pager), None)
        except api_exceptions.PermissionDenied:
            return CheckResult(
                name,
                True,
                "reachable; caller cannot list secrets (expected with per-secret "
                f"accessor grants) -- set {secret_key} to probe an actual secret",
            )
        return CheckResult(name, True, f"listed secrets in {project}")

    return _probe(name, probe)


def firestore_read(
    name: str,
    collection: str,
    database_key: Optional[str] = None,
    covered_by: Optional[str] = None,
):
    """A Firestore collection is readable.

    `database_key` names the CONFIGURATION KEY holding the database id, not the
    id itself -- unset, or naming `(default)`, means the `(default)` database.
    `covered_by` names the check that already probes `(default)`; when this
    probe resolves to `(default)` and another check has it covered, it reports
    healthy rather than paying for a second identical read.

    A read, not a write. There is no side-effect-free write to probe with, and a
    service that can read a document but not update it is a far rarer
    misconfiguration than one that cannot see Firestore at all -- Firestore IAM
    is granted per database, so the roles that permit the read permit the write.
    """

    def probe() -> CheckResult:
        database = str(get_config(database_key) or "") if database_key else ""
        if covered_by and (not database or database == DEFAULT_DATABASE):
            return CheckResult(name, True, f"{DEFAULT_DATABASE}, covered by {covered_by}")
        client = _firestore_client_for(database or DEFAULT_DATABASE)
        client.collection(collection).document(PROBE_DOCUMENT).get(timeout=PROBE_TIMEOUT_SECONDS)
        return CheckResult(
            name,
            True,
            f"read {collection}/{PROBE_DOCUMENT} from {database or DEFAULT_DATABASE}",
        )

    return _probe(name, probe)


def gcs_list(name: str, bucket_key: str):
    """A bucket the service READS is listable.

    A LIST, not a `bucket.exists()`: the latter needs `storage.buckets.get`,
    which `roles/storage.objectViewer` does not carry, so it would fail for a
    service that can read every object it will ever be asked for.
    """

    def probe() -> CheckResult:
        bucket = get_config(bucket_key)
        if not bucket:
            return CheckResult(name, False, f"{bucket_key} is not configured")
        blobs = _storage_client().list_blobs(bucket, max_results=1, timeout=PROBE_TIMEOUT_SECONDS)
        found = next(iter(blobs), None)
        return CheckResult(
            name,
            True,
            f"listed gs://{bucket} ({'not empty' if found is not None else 'empty'})",
        )

    return _probe(name, probe)


def gcs_write(name: str, bucket_key: str):
    """A bucket the service WRITES is writable.

    A real object write, because for a write-only bucket that is the only thing
    the service ever does to it and the only failure that matters: a results
    bucket that cannot be written is discovered today at the end of a job, after
    all the work has been done and there is nowhere to put the answer.

    `if_generation_match=0` makes it a create rather than an overwrite -- the
    same operation the service performs for every new object, so a service
    account holding only `objectCreator` passes. The object is deleted straight
    afterwards; if the deletion is denied (also `objectCreator`) that is logged,
    not failed, and the `_healthcheck/` prefix is there so a lifecycle rule can
    take care of it.
    """

    def probe() -> CheckResult:
        bucket_name = get_config(bucket_key)
        if not bucket_name:
            return CheckResult(name, False, f"{bucket_key} is not configured")

        blob = _storage_client().bucket(bucket_name).blob(f"{PROBE_PREFIX}{uuid.uuid4().hex}")
        blob.upload_from_string(
            b"",
            content_type="application/octet-stream",
            if_generation_match=0,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        try:
            blob.delete(timeout=PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning(
                f"health probe wrote gs://{bucket_name}/{blob.name} but could not delete it: "
                f"{type(exc).__name__}: {exc} -- consider a lifecycle rule on {PROBE_PREFIX}"
            )
        return CheckResult(name, True, f"wrote and removed an object in gs://{bucket_name}")

    return _probe(name, probe)


def custom(name: str, body: Callable[[], CheckResult]):
    """Wrap a service-specific probe so the engine can run it.

    The escape hatch for a dependency the builders above do not cover (a Cloud
    Tasks queue, a Valkey instance, another service's `/health`). `body` returns
    a `CheckResult`; raising is equivalent to returning a failed one.
    """
    return _probe(name, body)


# --- the engine -------------------------------------------------------------


def checks_enabled() -> bool:
    """Are the dependency probes switched on?

    `HEALTH_DEPENDENCY_CHECKS=false` turns them off everywhere -- both the
    startup gate and `/health`. It exists for running a service somewhere its
    dependencies genuinely are not reachable (a laptop, a unit test), not as a
    way to keep a broken deployment serving.
    """
    return str(get_config("HEALTH_DEPENDENCY_CHECKS", "true")).strip().lower() != "false"


def _run(name: str, probe: Callable[[], CheckResult]) -> CheckResult:
    """Run one probe, turning any exception into a failed result."""
    started = time.monotonic()
    try:
        result = probe()
    except Exception as exc:
        result = CheckResult(name, False, f"{type(exc).__name__}: {exc}")
    return CheckResult(result.name, result.ok, result.detail, (time.monotonic() - started) * 1000.0)


class Preflight:
    """One service's dependency checks: what it needs, and how to prove it.

    Held as an object rather than module state so the checks are passed in
    explicitly, a test can build a second one without disturbing the first, and
    the cached result belongs to the service rather than to the import.
    """

    def __init__(self, component: str, checks: Sequence[Callable[[], CheckResult]]):
        if not component:
            raise ValueError(
                "preflight needs a component name -- it prefixes every alert title, "
                "becomes a label, and salts the fingerprint"
            )
        self.component = component
        self.checks: Tuple[Tuple[str, Callable[[], CheckResult]], ...] = tuple(
            (probe.check_name, probe) for probe in checks
        )
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[float, bool, Dict[str, Any]]] = None

    # -- alerting ----------------------------------------------------------

    def _publish_component(self) -> None:
        """Name this service in the alert configuration.

        `setdefault`, which is the correct precedence and the correct moment:
        configuration has already been promoted into `os.environ` by the time a
        probe runs, so a value set by the deployment or by config still wins --
        and the alert config is built from the environment once, at the first
        report, which may be another subsystem's, not ours.
        """
        try:
            os.environ.setdefault("OPTERYX_ALERTS_COMPONENT", self.component)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"could not name this component for alerting: {exc}")

    @staticmethod
    def _alerts():
        """The catalog's alerting module, or None if it cannot be imported.

        Imported lazily, and its failure tolerated, so this works in a service
        that does not depend on `opteryx-catalog` at all (and so the query
        engine is not dragged in by a health request).
        """
        try:
            from opteryx_catalog import alerts

            return alerts
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"dependency alerting unavailable: {type(exc).__name__}: {exc}")
            return None

    def report_failures(self, results, phase: str, blocking: bool) -> None:
        """Raise an alert for each failed check, via the catalog's alert sinks.

        One report per failing check, not one for the set: the fingerprint
        carries the check name, so "Firestore is unreachable" and "the results
        bucket is not writable" stay separate tickets that recur and cool off
        independently. The phase is context rather than fingerprint -- the same
        broken grant found at startup and again on `/health` is one failure,
        not two.

        Severity is ERROR by the catalog's own taxonomy: work is being refused
        and data is intact. Note what that implies for delivery -- the Discord
        sink is gated at CRITICAL by default, so these reach stdout and GitHub
        but will only page anyone if the deployment sets
        `OPTERYX_ALERTS_DISCORD_MIN_SEVERITY` to `ERROR`.

        `blocking` delivers to the remote sinks inline instead of on the
        background worker. Startup sets it because the process is about to exit
        and a queued report would die with it; `/health` must not, or a GitHub
        API call would be on the request path.

        Never raises. An alerting failure must not be what makes the health
        endpoint 500 or the startup gate fail for the wrong reason -- and note
        that when it is Secret Manager that is down, the GitHub and Discord
        sinks cannot resolve their credentials either. The stdout record is the
        guarantee; a log-based alert policy on it is the backstop for exactly
        that case.
        """
        failures = [result for result in results if not result.ok]
        if not failures:
            return

        alerts = self._alerts()
        if alerts is None:
            return

        for result in failures:
            try:
                alerts.report(
                    DependencyUnavailable(f"{result.name}: {result.detail}"),
                    title=f"{self.component} cannot reach {result.name}",
                    note=(
                        f"The {self.component} service probes every dependency it needs, "
                        "at startup and on /health. This one failed, so requests routed "
                        "to this instance cannot be served."
                    ),
                    fingerprint=(f"{self.component}-dependency", result.name),
                    context={
                        "check": result.name,
                        "detail": result.detail,
                        "phase": phase,
                        "failed_checks": ", ".join(f.name for f in failures),
                    },
                    severity=alerts.AlertSeverity.ERROR,
                    labels=(self.component, "dependency"),
                    blocking=blocking,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"could not alert on {result.name}: {type(exc).__name__}: {exc}")

        if blocking:
            try:
                alerts.flush(timeout=PROBE_TIMEOUT_SECONDS)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"could not flush alerts: {type(exc).__name__}: {exc}")

    # -- running -----------------------------------------------------------

    def run_checks(self) -> List[CheckResult]:
        """Run every declared probe concurrently and return the results.

        Concurrent because they are independent network calls and startup pays
        for them: sequentially, five probes against an unreachable dependency
        would cost five timeouts before the container gave up.

        The executor is NOT context-managed on purpose -- `__exit__` joins its
        threads, which would hand a hung client the very unbounded wait
        `OVERALL_TIMEOUT_SECONDS` exists to prevent.
        """
        if not self.checks:
            return []
        executor = ThreadPoolExecutor(max_workers=len(self.checks), thread_name_prefix="preflight")
        try:
            futures = [(name, executor.submit(_run, name, probe)) for name, probe in self.checks]
            deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
            results = []
            for name, future in futures:
                try:
                    results.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
                except Exception as exc:
                    future.cancel()
                    results.append(
                        CheckResult(name, False, f"did not complete: {type(exc).__name__}: {exc}")
                    )
            return results
        finally:
            executor.shutdown(wait=False)

    def reset_cache(self) -> None:
        """Forget the cached result, so the next call probes for real."""
        with self._lock:
            self._cached = None

    def readiness(self, force: bool = False, phase: str = "health") -> Tuple[bool, Dict[str, Any]]:
        """Return `(healthy, payload)` for the dependency checks.

        Cached for `CACHE_TTL_SECONDS`; `force` skips the cache (startup does,
        so it never inherits a result from an import-time probe).

        `phase` says who is asking -- "startup" or "health". It reaches the
        alert as context, and it is what decides whether alert delivery blocks:
        a startup check is running in a process that is about to exit.

        Alerts are raised only when the probes ACTUALLY RUN, so a cached failure
        does not re-report; that bounds this to one report per failing check per
        `CACHE_TTL_SECONDS` before the alert module's own cooloff even applies.
        """
        if not checks_enabled():
            return True, {"status": "ok", "checks": "disabled"}

        with self._lock:
            cached = self._cached
        if not force and cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            payload = dict(cached[2])
            payload["cached_age_seconds"] = round(time.monotonic() - cached[0], 1)
            return cached[1], payload

        self._publish_component()
        results = self.run_checks()
        healthy = all(result.ok for result in results)
        if not healthy:
            self.report_failures(results, phase=phase, blocking=(phase == "startup"))
        payload: Dict[str, Any] = {
            "status": "ok" if healthy else "unavailable",
            "checks": [result.as_dict() for result in results],
            # Contains no configuration VALUES -- see `ConfigClient.status`.
            # Included because "the config document did not load" is the
            # explanation for most of the ways the checks above fail.
            "config": config_status(),
            "cached_age_seconds": 0.0,
        }

        with self._lock:
            self._cached = (time.monotonic(), healthy, payload)
        return healthy, payload

    def run_startup_checks(self) -> None:
        """Probe every dependency at startup; raise if the service cannot serve.

        Failures are alerted on (blocking, since the caller is usually about to
        exit) before the raise.

        Raising is what `main` turns into a non-zero container exit, which is
        what stops an instance that cannot serve from replacing one that can.
        Called from the ASGI lifespan as well, where it can only take one
        uvicorn child down -- see this module's docstring for why that is a
        backstop rather than the guarantee.
        """
        if not checks_enabled():
            logger.warning(
                "startup dependency checks are DISABLED (HEALTH_DEPENDENCY_CHECKS=false); "
                f"{self.component} will accept requests without proving it can serve them"
            )
            return

        healthy, payload = self.readiness(force=True, phase="startup")
        for check in payload.get("checks", []):
            line = f"startup check {check['name']}: {check['detail']} ({check['duration_ms']}ms)"
            if check["ok"]:
                logger.info(line)
            else:
                logger.error(line)

        if not healthy:
            failed = ", ".join(c["name"] for c in payload.get("checks", []) if not c["ok"])
            raise DependencyUnavailable(
                f"{self.component} cannot serve requests -- dependency checks failed: {failed}"
            )

        logger.info(f"startup dependency checks passed; {self.component} is able to serve")

    def main(self) -> int:
        """Exit 0 when every dependency is reachable, 1 when any is not.

        The individual results are logged by `run_startup_checks`, and the
        failures are alerted on.
        """
        try:
            self.run_startup_checks()
        except Exception as exc:
            # Already logged per check, and already alerted, by
            # `run_startup_checks`. This line is what an operator reads first in
            # the container's logs, so it says what happened rather than only
            # which checks failed.
            logger.error(
                f"preflight FAILED, refusing to start the server: {type(exc).__name__}: {exc}"
            )
            return 1
        return 0
