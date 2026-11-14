"""The platform maintenance switch: the matrix, the refusals, and the failure rule."""

import datetime

import pytest

from opteryx_shared_services import platform_mode as pm

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class Snapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class FakeDb:
    """`collection(...).document(...).get()` returning whatever `doc` holds now."""

    def __init__(self, doc=None):
        self.doc = doc
        self.fail = False
        self.reads = []

    def collection(self, name):
        self._collection = name
        return self

    def document(self, doc_id):
        self.reads.append((self._collection, doc_id))
        return self

    def get(self, timeout=None):
        if self.fail:
            raise RuntimeError("firestore unavailable")
        return Snapshot(self.doc)


def cfg(mode_map):
    """A configuration document carrying `mode_map` as its platform_mode field."""
    return {"values": {"GCS_BUCKET": "bucket"}, pm.FIELD: mode_map}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def db():
    return FakeDb()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def switch(db, clock, monkeypatch):
    monkeypatch.delenv(pm.ENV_OVERRIDE, raising=False)
    return pm.PlatformMode(
        environment="env-a",
        client_provider=lambda: db,
        clock=clock,
        promoted=lambda key, value: False,
    )


# --- the matrix --------------------------------------------------------------

R, W = pm.READ, pm.WRITE


@pytest.mark.parametrize(
    "service, mode, op, expected",
    [
        # operational: everything, everywhere
        *[(s, pm.OPERATIONAL, op, True) for s in pm.SERVICES for op in (R, W)],
        # read_only
        (pm.JOBS, pm.READ_ONLY, R, True),
        (pm.JOBS, pm.READ_ONLY, W, False),
        (pm.WORKER, pm.READ_ONLY, R, True),
        (pm.WORKER, pm.READ_ONLY, W, False),
        (pm.FLIGHT, pm.READ_ONLY, R, True),
        (pm.FLIGHT, pm.READ_ONLY, W, False),
        (pm.ODATA, pm.READ_ONLY, R, True),
        (pm.ODATA, pm.READ_ONLY, W, False),
        (pm.UPLOAD, pm.READ_ONLY, W, False),
        (pm.CONTROL, pm.READ_ONLY, R, False),
        (pm.CONTROL, pm.READ_ONLY, W, False),
        (pm.XB500, pm.READ_ONLY, R, True),
        (pm.XB500, pm.READ_ONLY, W, False),
        # reject: nothing, anywhere
        *[(s, pm.REJECT, op, False) for s in pm.SERVICES for op in (R, W)],
        # queue: only jobs accepts; the worker holds it
        (pm.JOBS, pm.QUEUE, R, True),
        (pm.JOBS, pm.QUEUE, W, True),
        *[(s, pm.QUEUE, op, False) for s in pm.SERVICES if s != pm.JOBS for op in (R, W)],
    ],
)
def test_matrix(service, mode, op, expected):
    assert pm.allowed(mode, service, op) is expected


def test_matrix_covers_every_service_and_mode():
    assert set(pm.ALLOWED) == set(pm.SERVICES)
    for service, modes in pm.ALLOWED.items():
        assert set(modes) == set(pm.MODES), service


@pytest.mark.parametrize(
    "args",
    [("read_only", "nope", R), ("read_only", pm.JOBS, "delete"), ("readonly", pm.JOBS, R)],
)
def test_unknown_names_are_a_caller_bug(args):
    with pytest.raises(ValueError):
        pm.allowed(*args)


# --- refusals ----------------------------------------------------------------


def test_allowed_operation_has_no_refusal():
    assert pm.refusal_for(pm.State(mode=pm.READ_ONLY), pm.FLIGHT, R, NOW) is None


def test_refusal_carries_message_reason_and_default_retry():
    state = pm.State(mode=pm.REJECT, reason="catalog migration")
    refusal = pm.refusal_for(state, pm.ODATA, R, NOW)
    assert refusal.mode == pm.REJECT
    assert refusal.reason == "catalog migration"
    assert refusal.message.startswith(pm.MESSAGES[pm.REJECT])
    assert "catalog migration" in refusal.message
    assert refusal.retry_after_seconds == pm.DEFAULT_RETRY_AFTER_SECONDS


def test_retry_after_counts_down_to_until():
    state = pm.State(mode=pm.REJECT, until=NOW + datetime.timedelta(minutes=30))
    assert pm.refusal_for(state, pm.ODATA, R, NOW).retry_after_seconds == 1800


def test_until_in_the_past_falls_back_to_default_retry():
    state = pm.State(mode=pm.REJECT, until=NOW - datetime.timedelta(minutes=1))
    assert pm.refusal_for(state, pm.ODATA, R, NOW).retry_after_seconds == (
        pm.DEFAULT_RETRY_AFTER_SECONDS
    )


# --- reading the document ----------------------------------------------------


def test_no_document_is_operational(switch, db):
    state = switch.current()
    assert state.mode == pm.OPERATIONAL
    assert state.source == "document"
    assert db.reads == [(pm.COLLECTION, "env-a")]


def test_document_mode_and_fields_are_read(switch, db):
    db.doc = cfg({"mode": "read_only", "reason": "migration", "until": "2026-11-14T13:00:00+00:00"})
    state = switch.current()
    assert state.mode == pm.READ_ONLY
    assert state.reason == "migration"
    assert state.until == datetime.datetime(2026, 11, 14, 13, tzinfo=UTC)
    assert switch.check(pm.UPLOAD, W) is not None
    assert switch.check(pm.FLIGHT, R) is None


def test_document_is_cached_for_the_ttl(switch, db, clock):
    db.doc = cfg({"mode": "reject"})
    assert switch.current().mode == pm.REJECT
    db.doc = cfg({"mode": "operational"})
    clock.t += pm.CACHE_TTL_SECONDS - 1
    assert switch.current().mode == pm.REJECT
    assert len(db.reads) == 1
    clock.t += 1
    assert switch.current().mode == pm.OPERATIONAL
    assert len(db.reads) == 2


def test_read_error_keeps_the_last_mode_not_operational(switch, db, clock):
    db.doc = cfg({"mode": "reject"})
    assert switch.current().mode == pm.REJECT
    db.fail = True
    clock.t += pm.CACHE_TTL_SECONDS
    state = switch.current()
    assert state.mode == pm.REJECT
    assert state.source == "document"


def test_read_error_before_any_success_is_operational(switch, db):
    db.fail = True
    state = switch.current()
    assert state.mode == pm.OPERATIONAL
    assert state.source == "default"


def test_read_error_is_retried_once_per_ttl(switch, db, clock):
    db.fail = True
    switch.current()
    switch.current()
    assert len(db.reads) == 1
    db.fail = False
    db.doc = cfg({"mode": "queue"})
    clock.t += pm.CACHE_TTL_SECONDS
    assert switch.current().mode == pm.QUEUE


def test_unknown_mode_in_document_keeps_the_last_mode(switch, db, clock):
    db.doc = cfg({"mode": "read_only"})
    switch.current()
    db.doc = cfg({"mode": "readonly"})  # a typo made in a hurry
    clock.t += pm.CACHE_TTL_SECONDS
    assert switch.current().mode == pm.READ_ONLY


def test_config_document_without_the_field_is_operational(switch, db):
    db.doc = {"values": {"PLATFORM_MODE": "reject"}}
    assert switch.current().mode == pm.OPERATIONAL


def test_reads_the_environments_config_document(switch, db):
    switch.current()
    assert db.reads == [("config", "env-a")]


def test_malformed_field_keeps_the_last_mode(switch, db, clock):
    db.doc = cfg({"mode": "reject"})
    switch.current()
    db.doc = {pm.FIELD: "reject"}  # a string, not a map
    clock.t += pm.CACHE_TTL_SECONDS
    assert switch.current().mode == pm.REJECT


def test_deleting_the_field_returns_to_operational(switch, db, clock):
    db.doc = cfg({"mode": "reject"})
    switch.current()
    db.doc = {"values": {}}
    clock.t += pm.CACHE_TTL_SECONDS
    assert switch.current().mode == pm.OPERATIONAL


def test_deleting_the_document_returns_to_operational(switch, db, clock):
    db.doc = cfg({"mode": "reject"})
    switch.current()
    db.doc = None
    clock.t += pm.CACHE_TTL_SECONDS
    assert switch.current().mode == pm.OPERATIONAL


def test_refresh_forces_a_read(switch, db):
    db.doc = cfg({"mode": "reject"})
    switch.current()
    db.doc = cfg({"mode": "operational"})
    switch.refresh()
    assert switch.current().mode == pm.OPERATIONAL


def test_snapshot_from_a_listener_applies_immediately(switch, db):
    switch.current()
    switch.set_mode_from_snapshot(Snapshot(cfg({"mode": "queue"})))
    assert switch.current().mode == pm.QUEUE
    assert len(db.reads) == 1


# --- the environment override ------------------------------------------------


def test_env_override_wins_and_skips_the_read(switch, db, monkeypatch):
    db.doc = cfg({"mode": "operational"})
    monkeypatch.setenv(pm.ENV_OVERRIDE, "Reject")
    state = switch.current()
    assert state.mode == pm.REJECT
    assert state.source == "env"
    assert db.reads == []


def test_unknown_env_override_is_ignored(switch, db, monkeypatch, caplog):
    db.doc = cfg({"mode": "read_only"})
    monkeypatch.setenv(pm.ENV_OVERRIDE, "off")
    assert switch.current().mode == pm.READ_ONLY
    switch.current()
    assert sum("ignoring PLATFORM_MODE" in r.message for r in caplog.records) == 1


def test_override_promoted_from_config_values_is_ignored(db, clock, monkeypatch, caplog):
    """Promotion freezes a value at boot; only the deployment's own variable counts."""
    monkeypatch.setenv(pm.ENV_OVERRIDE, "reject")
    switch = pm.PlatformMode(
        environment="env-a",
        client_provider=lambda: db,
        clock=clock,
        promoted=lambda key, value: key == pm.ENV_OVERRIDE and value == "reject",
    )
    db.doc = cfg({"mode": "read_only"})
    assert switch.current().mode == pm.READ_ONLY
    assert any("promoted" in r.message for r in caplog.records)


def test_promotion_check_uses_the_config_client(monkeypatch):
    from opteryx_shared_services import config

    monkeypatch.setitem(config._default_client._promoted, pm.ENV_OVERRIDE, "reject")
    assert pm._promoted_by_config(pm.ENV_OVERRIDE, "reject") is True
    assert pm._promoted_by_config(pm.ENV_OVERRIDE, "queue") is False
