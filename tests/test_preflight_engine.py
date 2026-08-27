"""A service must not accept work it cannot serve.

These cover the guarantees the engine makes for every service that uses it: a
failed dependency check takes startup down (rather than producing an instance
that accepts requests and fails them one at a time), the failure is alerted on
exactly once, and `main()` turns it into the non-zero exit that fails a Cloud
Run revision.
"""

import pytest

from opteryx_shared_services import preflight
from opteryx_shared_services.preflight import Preflight


def _ok(name="a-check", detail="fine"):
    return preflight.custom(name, lambda: preflight.CheckResult(name, True, detail))


def _bad(name="a-check", detail="PermissionDenied"):
    return preflight.custom(name, lambda: preflight.CheckResult(name, False, detail))


# --- the declaration contract ----------------------------------------------


def test_a_service_must_name_itself():
    """The component prefixes every alert title, becomes a label and salts the
    fingerprint -- unnamed, a service's alerts are unattributable and collide
    with every other service reporting into the same repo."""
    with pytest.raises(ValueError):
        Preflight(component="", checks=(_ok(),))


def test_checks_are_declared_explicitly_and_keep_their_order():
    service = Preflight(component="worker", checks=(_ok("first"), _ok("second")))

    assert [name for name, _ in service.checks] == ["first", "second"]


def test_two_services_do_not_share_a_cached_result():
    """The cache belongs to the service, not to the import."""
    one = Preflight(component="one", checks=(_ok(),))
    two = Preflight(component="two", checks=(_bad(),))

    assert one.readiness()[0]
    assert not two.readiness()[0]


# --- the engine -------------------------------------------------------------


def test_healthy_when_every_check_passes():
    healthy, payload = Preflight("worker", (_ok("gcs-data"), _ok("gcs-results"))).readiness(
        force=True
    )

    assert healthy
    assert payload["status"] == "ok"
    assert [c["name"] for c in payload["checks"]] == ["gcs-data", "gcs-results"]


def test_one_failure_makes_the_service_unhealthy():
    healthy, payload = Preflight("worker", (_ok("gcs-data"), _bad("gcs-results"))).readiness(
        force=True
    )

    assert not healthy
    assert payload["status"] == "unavailable"


def test_a_raising_probe_is_a_failed_check_not_a_crash():
    def explode():
        raise RuntimeError("the client blew up")

    healthy, payload = Preflight(
        "worker", (preflight.custom("firestore-jobs", explode),)
    ).readiness(force=True)

    assert not healthy
    assert "the client blew up" in payload["checks"][0]["detail"]


def test_results_are_cached_so_health_cannot_be_used_to_bill_the_project():
    calls = []

    def counted():
        calls.append(1)
        return preflight.CheckResult("gcs-results", True, "wrote")

    service = Preflight("worker", (preflight.custom("gcs-results", counted),))

    service.readiness()
    service.readiness()
    service.readiness()

    assert len(calls) == 1

    service.readiness(force=True)

    assert len(calls) == 2


def test_checks_can_be_switched_off(config):
    config(HEALTH_DEPENDENCY_CHECKS="false")
    service = Preflight("worker", (_bad(),))

    assert not preflight.checks_enabled()
    healthy, payload = service.readiness(force=True)

    assert healthy
    assert payload["checks"] == "disabled"


# --- startup ----------------------------------------------------------------


def test_startup_raises_when_a_dependency_is_unreachable():
    service = Preflight("worker", (_bad("firestore-jobs", "ServiceUnavailable"),))

    with pytest.raises(preflight.DependencyUnavailable) as raised:
        service.run_startup_checks()

    assert "firestore-jobs" in str(raised.value)
    assert "worker" in str(raised.value)


def test_startup_returns_when_everything_is_reachable():
    Preflight("worker", (_ok(),)).run_startup_checks()  # does not raise


def test_startup_skipped_when_checks_are_disabled(config):
    config(HEALTH_DEPENDENCY_CHECKS="false")

    Preflight("worker", (_bad(),)).run_startup_checks()  # does not raise


def test_exits_zero_when_the_dependencies_are_reachable():
    assert Preflight("worker", (_ok(),)).main() == 0


def test_exits_non_zero_when_a_dependency_is_not():
    """The exit code is what fails a Cloud Run revision and keeps traffic on the
    one already serving -- an ASGI lifespan cannot give that guarantee, because
    uvicorn's supervisor binds the socket in the parent and respawns children."""
    assert Preflight("worker", (_bad(),)).main() == 1


# --- alerting ---------------------------------------------------------------


def test_a_failed_check_raises_an_alert(alerts):
    Preflight("upload", (_ok("gcs-data"), _bad("gcs-results", "PermissionDenied"))).readiness(
        force=True
    )

    assert len(alerts.reports) == 1
    exc, kwargs = alerts.reports[0]
    assert isinstance(exc, preflight.DependencyUnavailable)
    assert kwargs["title"] == "upload cannot reach gcs-results"
    assert kwargs["fingerprint"] == ("upload-dependency", "gcs-results")
    assert kwargs["labels"] == ("upload", "dependency")


def test_every_failed_check_is_reported_separately(alerts):
    """Separate fingerprints, so the two failures recur and cool off apart."""
    Preflight("worker", (_bad("firestore-jobs"), _bad("gcs-results"))).readiness(force=True)

    assert {kwargs["fingerprint"][1] for _, kwargs in alerts.reports} == {
        "firestore-jobs",
        "gcs-results",
    }


def test_two_services_reporting_the_same_check_stay_distinguishable(alerts):
    Preflight("upload", (_bad("gcs-data"),)).readiness(force=True)
    Preflight("worker", (_bad("gcs-data"),)).readiness(force=True)

    assert {kwargs["fingerprint"] for _, kwargs in alerts.reports} == {
        ("upload-dependency", "gcs-data"),
        ("worker-dependency", "gcs-data"),
    }


def test_a_healthy_service_alerts_nobody(alerts):
    Preflight("worker", (_ok(),)).readiness(force=True)

    assert alerts.reports == []


def test_a_cached_failure_does_not_re_alert(alerts):
    service = Preflight("worker", (_bad(),))

    service.readiness()
    service.readiness()
    service.readiness()

    assert len(alerts.reports) == 1


def test_startup_delivers_its_alert_inline(alerts):
    """The process is about to exit; a queued report would die with it."""
    with pytest.raises(preflight.DependencyUnavailable):
        Preflight("worker", (_bad("firestore-jobs"),)).run_startup_checks()

    assert alerts.reports[0][1]["blocking"] is True
    assert alerts.flushed


def test_health_does_not_block_on_alert_delivery(alerts):
    """A GitHub API call must not be on the request path."""
    Preflight("worker", (_bad("gcs-results"),)).readiness(force=True)

    assert alerts.reports[0][1]["blocking"] is False


def test_alerting_that_is_itself_broken_does_not_break_the_check(monkeypatch):
    class _Broken:
        class AlertSeverity:
            ERROR = "ERROR"

        def report(self, *args, **kwargs):
            raise RuntimeError("the sink is down")

        def flush(self, timeout=None):
            raise RuntimeError("the sink is still down")

    monkeypatch.setattr(Preflight, "_alerts", staticmethod(lambda: _Broken()))

    healthy, payload = Preflight("worker", (_bad("secret-manager"),)).readiness(force=True)

    assert not healthy
    assert payload["checks"][0]["name"] == "secret-manager"


def test_missing_alert_module_does_not_break_the_check(monkeypatch):
    monkeypatch.setattr(Preflight, "_alerts", staticmethod(lambda: None))

    assert not Preflight("worker", (_bad("gcs-data", "NotFound"),)).readiness(force=True)[0]
