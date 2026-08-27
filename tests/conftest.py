import os
import time

# Import-time priming would try to reach Firestore (and, before that, the
# metadata server) as a side effect of importing the module under test. Tests
# drive `prime()` explicitly instead.
os.environ["OPTERYX_CONFIG_EAGER"] = "false"

import pytest

from opteryx_shared_services import config as opteryx_config


@pytest.fixture(autouse=True)
def no_metadata_probe():
    """Pretend the metadata server has already been probed and was absent.

    Without this, any test that reads `.project` or an undetected environment
    makes a real network call. The metadata tests reset the cache themselves.

    Both halves of the state are needed: a failed probe is only trusted for
    `_METADATA_RETRY_SECONDS`, so a stale-looking timestamp would let a real
    probe through.
    """
    opteryx_config._METADATA_PROJECT = None
    opteryx_config._METADATA_FAILED_AT = time.monotonic()
    yield
    opteryx_config._METADATA_PROJECT = None
    opteryx_config._METADATA_FAILED_AT = None


@pytest.fixture(autouse=True)
def restore_environ():
    """Undo anything a test left in `os.environ`.

    `promote_to_environ` writes there directly, outside monkeypatch's records,
    so without this a promoted key leaks into every test that runs after it --
    and a leaked key looks exactly like a deployment ENVVAR, which is the one
    thing these tests are distinguishing.
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


# --- preflight ---------------------------------------------------------------

from opteryx_shared_services import preflight as _preflight  # noqa: E402


@pytest.fixture(autouse=True)
def config(monkeypatch):
    """Stub `get_config` for the preflight probes and return a setter.

    Autouse: a probe that reached the real client would try to read the
    configuration document, and through it Firestore, from a unit test.
    """
    values = {}
    monkeypatch.setattr(
        _preflight, "get_config", lambda key, default=None: values.get(key, default)
    )

    def _set(**kwargs):
        values.update(kwargs)

    return _set


class FakeAlerts:
    """Stands in for `opteryx_catalog.alerts`, recording what was reported."""

    class AlertSeverity:
        WARNING = "WARNING"
        ERROR = "ERROR"
        CRITICAL = "CRITICAL"

    def __init__(self):
        self.reports = []
        self.flushed = False

    def report(self, exc, **kwargs):
        self.reports.append((exc, kwargs))

    def flush(self, timeout=None):
        self.flushed = True


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    """No test reaches the real alert sinks; the checks-fail tests read this."""
    fake = FakeAlerts()
    monkeypatch.setattr(_preflight.Preflight, "_alerts", staticmethod(lambda: fake))
    return fake
