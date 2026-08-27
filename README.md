# opteryx-shared-services

Capabilities every Opteryx service needs, in one package.

```bash
pip install opteryx-shared-services
```

Three seed capabilities, each of which was previously copied into eight service
repos and had silently diverged there:

| module | provides |
| --- | --- |
| `opteryx_shared_services.config` | the platform configuration client |
| `opteryx_shared_services.logging` | structured logging, and the audit wire format the fleet's log ingestion selects on |
| `opteryx_shared_services.preflight` | startup dependency checks that fail a Cloud Run revision rather than letting an instance that cannot serve replace one that can |
| `opteryx_shared_services.audit` | request audit middleware, and the payload shape the fleet's log ingestion reads |

Nothing is imported by the top-level package. A service that needs only
configuration must not pay for a Firestore, GCS or Secret Manager import it
never uses, so each capability is imported by name.

## Why a package, not vendored copies

The previous mechanism was copy-by-`vendor.sh`. It does not hold. At the point
this package was created, `config_client.py` existed in **four different
versions** across eight repos, and **five of the eight** were missing
`promote_to_environ` — the function that copies the configuration document into
`os.environ`, which is the only way `opteryx.config` and `opteryx_catalog` (both
of which read `os.environ` directly) ever see it. One repo had been re-vendored
two days earlier and *still* lacked it, because it was synced from a stale
checkout.

Nothing could detect that, because a copy carries no version. A pinned
dependency answers "which version is this service on?" by reading
`pyproject.toml`.

## `config`

```python
from opteryx_shared_services.config import get_config

AUTH_URL = get_config("AUTH_URL", default="https://authenticate.opteryx.app")
```

Configuration lives in Firestore at `(default)/config/<environment>`, one
document per environment. Resolution order is process environment → the
configuration document → the caller's default, so a value set in the service's
ENVVARs always wins.

The document is read once at import and every value copied into `os.environ`
without overwriting anything already there — that is what reaches code which
cannot call `get_config` (`opteryx.config`, `opteryx_catalog`). The client is
deliberately fail-open: if Firestore is unreachable it serves last-known values
and otherwise the caller's default, so a Firestore blip cannot take down the
fleet.

`OPTERYX_CONFIG_EAGER=false` skips the read at import;
`OPTERYX_CONFIG_PROMOTE=false` skips the copy into `os.environ`.

## `logging`

```python
from opteryx_shared_services.logging import get_logger, set_log_name

set_log_name("opteryx.worker")
logger = get_logger()
```

**The audit wire format is a cross-service contract.** `xb500.opteryx`'s
`transform_audit_logs` ingests Cloud Logging entries whose
`jsonPayload.severity` is `"AUDIT"` into `ops.audit_log`;
`transform_billing_logs` selects `"BILLING"` the same way. That imposes three
requirements on every `logger.audit(...)` call:

1. the line must be **valid JSON on its own** — Cloud Logging only parses a
   stdout line into `jsonPayload` when the whole line is JSON, so a
   `time | LEVEL | name | {...}` prefix makes it opaque `textPayload`;
2. the payload must carry **`severity` itself** — the LogEntry's GCP-assigned
   severity is not what the transform selects on;
3. a caller-supplied severity must **survive verbatim** — billing events pass
   `severity="BILLING"` and are ingested by a different transform.

So audit and alert records get their own stdout handler with no formatter
prefix, and their payload is passed through untouched. Ordinary records go to a
separate handler that never sees them.

Ordinary records are JSON with a mapped `severity` under `K_SERVICE` (every
Cloud Run deployment), because formatted text lands as `textPayload` at DEFAULT
severity where severity-based alerting cannot see even an ERROR. Everywhere else
they stay human-readable. `LOG_FORMAT=json` / `LOG_FORMAT=text` overrides either
way.

Also included: `set_trace_context()` for request correlation, `extra=` fields
riding along as their own `jsonPayload` fields, tracebacks folded into `message`
so Error Reporting groups them, `propagate = False` so uvicorn does not
duplicate every line, and third-party loggers (`opteryx.config_client`,
`opteryx_catalog`) adopted at WARNING so a broken alert sink cannot present as
a channel simply being quiet.

### What this fixed

At the point of consolidation, five of eight services emitted audit records the
audit pipeline could not see:

| service | was | now |
| --- | --- | --- |
| control, register | correct | unchanged |
| xb500 | bypassed logging via `opteryx_catalog.audit` | either route works |
| worker | JSON, but AUDIT mapped to severity `"NOTICE"` | `"AUDIT"` |
| jobs, upload, authenticate | text formatter → `textPayload` | raw JSON on stdout |
| odata | no handler at all → root logger | raw JSON on stdout |

Adopting this module therefore **starts new records flowing** into
`ops.audit_log` from five services. Validate their payloads against
`AUDIT_LOG_SCHEMA` before rolling out.

## `audit`

```python
from opteryx_shared_services.audit import AuditMiddleware

service.add_middleware(AuditMiddleware)
```

Ten fields, the same for every service — `method`, `path`, `status`,
`duration_ms`, `from`, `timestamp`, `detail`, `jwt_present`/`jwt_sub`, and
`client_ip`/`user_agent`/`host`. The eight implementations this replaces had
diverged into eight payload shapes with only the first six in common, so
`ops.audit_log` could be joined on very little.

`from` is `x-forwarded-for` — what a proxy claims. `client_ip` is the peer the
server actually saw. They differ exactly when it matters.

### Why pure ASGI, not `BaseHTTPMiddleware`

`BaseHTTPMiddleware.call_next()` returns as soon as the status and headers are
captured — it cannot see whether the body finished streaming. A response that
started as a clean 200 and then died mid-stream (compression crash, worker OOM,
dropped connection) was audited as a plain `200 okay`, indistinguishable from a
full success, because the record was written before the body was ever sent.
**Seven of the eight services had that defect.**

This wraps `send` directly, tracking bytes actually written and whether the
stream was cleanly terminated, and reports a distinct detail when it was not.
It also re-raises rather than swallowing exceptions into a synthetic empty
Response, so Starlette's `ServerErrorMiddleware` produces a real error.

An exception reaching the middleware is *not* by itself a truncation —
`ServerErrorMiddleware` renders a complete 500 and then re-raises, and calling
that a transport failure would misdescribe a client that got a whole, correct
error response. Only an unterminated stream is reported as one.

### Extension points

Rather than forking the payload:

- `request.state.audit_extra = {...}` — any route may set this while handling
  the request; it is merged into the record. This is how a handler reports what
  it did without the middleware knowing anything about that route.
- `AuditMiddleware(app, extra_fields=fn)` — a callable taking the `Request`,
  for values derived from the request or environment.

Neither may overwrite a standard field, and a raising `extra_fields` is logged
and ignored: an incomplete audit record beats no record, and beats a 500 caused
by the auditing itself.

Needs the `web` extra (`starlette`).

## `preflight`

Each service keeps a small `app/preflight.py` that declares what it needs:

```python
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
```

Then run it *before* the server, with `&&` so a non-zero preflight means the
server is never `exec`'d:

```dockerfile
CMD python -m app.preflight && exec python -m uvicorn app.main:service --host 0.0.0.0 --port ${PORT:-8080}
```

`app/main.py`'s lifespan calls `preflight.run_startup_checks()`; the health route
calls `preflight.readiness()` and answers 503 when it is false. Keep
`/health/live` as a separate dependency-free ping, so a transient Firestore blip
does not get a busy instance killed mid-request.

### Why a separate process, not just the ASGI lifespan

A container running `uvicorn --workers N` has a supervisor that binds the
listening socket in the **parent** and respawns any child that dies. A lifespan
that raises kills one child, the parent starts another, and the port stays open
throughout — the revision looks alive while no worker can serve a thing.

`python -m app.preflight` runs before uvicorn exists. Its exit code is what fails
the revision. Keep the lifespan check too, as a second line of defence, but it is
not what makes the container fail.

### Probe builders

| builder | proves | use for |
| --- | --- | --- |
| `secret_manager(name=…, secret_key=…)` | Secret Manager reachable; reads a named secret when one is configured | any service that resolves secrets |
| `firestore_read(name, collection=…, database_key=…, covered_by=…)` | a collection is readable | every Firestore-backed service |
| `gcs_list(name, bucket_key=…)` | a bucket is listable | buckets the service **reads** |
| `gcs_write(name, bucket_key=…)` | an object can be created (and is then removed) | buckets the service **writes** |
| `custom(name, body)` | whatever `body` returns a `CheckResult` for | Cloud Tasks queues, Valkey, another service's `/health` |

Builders take **configuration keys**, not values. The name is resolved when the
probe runs, because configuration is promoted into `os.environ` during startup —
a value captured at import can be older than the one the rest of the process is
using.

`bucket_key` also accepts an ordered list, for a service that resolves its
bucket with a fallback — `bucket_key=("DATA_BUCKET", "GCS_BUCKET")` probes the
first one configured, and the detail names which key answered. Pass the same
order the service itself resolves, or the probe proves the wrong bucket.

**Declare the direction the service uses, not both.** A LIST is not a substitute
for a CREATE and vice versa: a service account holding `objectCreator` on a
results bucket (enough to serve every request) fails a read probe, and one
holding only `objectViewer` sails through a read probe and then fails at the
first upload. `bucket.exists()` is worse than either — it needs
`storage.buckets.get`, which `objectViewer` does not carry.

### Cost and safety

Probes make real RPCs, one of them a write, and `/health` is usually
unauthenticated — so results are cached for 30 seconds. Without that the endpoint
would be a way to bill the project by curl loop. Alerts are raised only when the
probes actually run, so a cached failure does not re-report.

Each probe is bounded at 5s and the whole set at 15s — the latter because a
client that hangs *before* it makes an RPC (ADC discovery, metadata server) never
reaches the per-call timeout. The write probe puts its object under
`_healthcheck/`, so a bucket lifecycle rule can mop up if the service account
cannot delete what it creates.

`HEALTH_DEPENDENCY_CHECKS=false` turns the probes off everywhere — both the
startup gate and `/health`. It is for running a service somewhere its
dependencies genuinely are not reachable (a laptop, a unit test), not a way to
keep a broken deployment serving.

## Optional extras

Only the config client's Firestore dependency is unconditional. The preflight
probes import their clients lazily, inside the probe that needs them:

```bash
pip install "opteryx-shared-services[storage,secrets,web]"
```

`web` is `starlette`, needed only by `audit`.

## Releasing

Tag `version-<pyproject version>`. The workflow gates on the full test matrix
(3.11–3.14) and refuses a tag that does not match `pyproject.toml`.

## Tests

```bash
python -m pip install -e ".[test,storage,secrets,web]"
python -m pytest tests -q
```
