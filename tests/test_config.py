"""Tests for the opteryx_config client resolution hierarchy, caching and reads."""

import json
import os
import time
from unittest import mock

import pytest

from opteryx_shared_services import config as opteryx_config
from opteryx_shared_services.config import ConfigClient


class _FakeFirestore:
    """Stands in for a Firestore client along the `collection/document/get` path."""

    def __init__(self, values=None, exists=True, error=None):
        self._values = {} if values is None else values
        self._exists = exists
        self._error = error
        self.reads = 0
        self.collections = []
        self.documents = []
        self.timeouts = []

    def collection(self, name):
        self.collections.append(name)
        return self

    def document(self, document_id):
        self.documents.append(document_id)
        return self

    def get(self, timeout=None):
        self.reads += 1
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        snapshot = mock.Mock()
        snapshot.exists = self._exists
        snapshot.to_dict.return_value = {"values": self._values} if self._exists else None
        return snapshot


def _make_client(values=None, ttl=60.0, exists=True, error=None, environment="testenv"):
    """A client reading from a fake Firestore, so no real client is constructed."""
    firestore = _FakeFirestore(values=values, exists=exists, error=error)
    client = ConfigClient(environment=environment, cache_ttl=ttl, client_provider=lambda: firestore)
    return client, firestore


# --- resolution hierarchy -------------------------------------------------


def test_envvar_takes_precedence(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://local.example")
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    assert client.get("AUTH_URL") == "https://local.example"


def test_config_value_used_when_no_envvar(monkeypatch):
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    assert client.get("AUTH_URL") == "https://remote.example"


def test_default_used_when_missing_everywhere(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    client, _ = _make_client({})
    assert client.get("MISSING_KEY", default="fallback") == "fallback"


# --- document location ----------------------------------------------------


def test_reads_environment_document_in_config_collection(monkeypatch):
    monkeypatch.delenv("KEY", raising=False)
    client, firestore = _make_client({"KEY": "v"}, environment="opteryx-prod")
    client.get("KEY")
    assert firestore.collections == ["config"]
    assert firestore.documents == ["opteryx-prod"]


def test_read_is_bounded_by_a_timeout(monkeypatch):
    """An unbounded read would hang container startup, since priming is at import."""
    monkeypatch.delenv("KEY", raising=False)
    client, firestore = _make_client({"KEY": "v"})
    client.get("KEY")
    assert firestore.timeouts == [opteryx_config.READ_TIMEOUT_SECONDS]


def test_missing_document_still_serves_defaults(monkeypatch):
    """No document must not break the caller. Fail-open is the whole design."""
    monkeypatch.delenv("KEY", raising=False)
    client, _ = _make_client(exists=False)
    assert client.get("KEY", default="d") == "d"


def test_missing_document_is_reported_as_a_failed_read(monkeypatch):
    """A document that is not there is not a successful read.

    It used to report `last_fetch_ok = True` on the reasoning that the gRPC call
    succeeded. But the caller's question is "is my configuration in effect", and
    the answer is no - so nothing logged, `status()` said the read was fine, and
    a service pointed at the wrong database or a mistyped document id served
    defaults indefinitely while looking healthy. The two likeliest
    misconfigurations were the two least visible.
    """
    monkeypatch.delenv("KEY", raising=False)
    client, _ = _make_client(exists=False)
    client.get("KEY", default="d")

    status = client.status()

    assert status["last_fetch_ok"] is False
    # Only the flag changes. The wording is allowlisted downstream as safe to
    # echo, and the path a reader needs is already on `status()` as
    # collection/environment/database/project.
    assert status["last_fetch_detail"] == "environment has no configuration document"
    assert (status["collection"], status["environment"]) == ("config", "testenv")


def test_malformed_values_field_ignored(monkeypatch):
    """A document whose `values` is not a mapping must not break every lookup."""
    monkeypatch.delenv("KEY", raising=False)
    client, _ = _make_client(values=["not", "a", "mapping"])
    assert client.get("KEY", default="d") == "d"


# --- caching --------------------------------------------------------------


def test_document_cached_within_ttl():
    client, firestore = _make_client({"KEY": "value"})
    client.get("KEY")
    client.get("KEY")
    client.get("KEY")
    assert firestore.reads == 1


def test_document_reread_after_ttl():
    client, firestore = _make_client({"KEY": "value"}, ttl=0.05)
    client.get("KEY")
    time.sleep(0.06)
    client.get("KEY")
    assert firestore.reads == 2


def test_refresh_forces_reread():
    client, firestore = _make_client({"KEY": "value"})
    client.get("KEY")
    client.refresh()
    client.get("KEY")
    assert firestore.reads == 2


def test_default_ttl_is_five_minutes():
    assert opteryx_config.CACHE_TTL_SECONDS == 300.0


# --- priming at boot ------------------------------------------------------


def test_prime_reads_before_first_lookup(monkeypatch):
    monkeypatch.delenv("KEY", raising=False)
    client, firestore = _make_client({"KEY": "v"})
    client.prime()
    assert firestore.reads == 1
    assert client.get("KEY") == "v"
    assert firestore.reads == 1, "the primed document should serve the first lookup"


def test_prime_failure_does_not_raise(monkeypatch):
    """A service must still start when Firestore is unreachable at boot."""
    monkeypatch.delenv("KEY", raising=False)
    client, _ = _make_client(error=RuntimeError("firestore down"))
    client.prime()
    assert client.get("KEY", default="d") == "d"


# --- failure behaviour ----------------------------------------------------


def test_stale_values_served_on_read_failure():
    client, firestore = _make_client({"KEY": "value"}, ttl=0.05)
    assert client.get("KEY") == "value"
    time.sleep(0.06)
    firestore._error = RuntimeError("firestore down")
    assert client.get("KEY") == "value"


def test_no_environment_falls_back_to_default(monkeypatch):
    """With no environment detectable, lookups must not hang or raise."""
    for var in ("OPTERYX_ENVIRONMENT", "GCP_PROJECT_ID", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("SOME_KEY", raising=False)
    monkeypatch.setattr(opteryx_config, "_METADATA_PROJECT", None)

    firestore = _FakeFirestore({"SOME_KEY": "unreachable"})
    client = ConfigClient(client_provider=lambda: firestore)
    assert client.get("SOME_KEY", default="fallback") == "fallback"
    assert firestore.reads == 0


def test_client_rebuilt_after_read_failure():
    """A broken gRPC channel must not be reused for every later read."""
    clients = [
        _FakeFirestore(error=RuntimeError("channel closed")),
        _FakeFirestore({"KEY": "recovered"}),
    ]
    client = ConfigClient(
        environment="testenv", cache_ttl=0.05, client_provider=lambda: clients.pop(0)
    )
    assert client.get("KEY", default="d") == "d"
    time.sleep(0.06)
    assert client.get("KEY") == "recovered"


# --- outage backoff (regression) ------------------------------------------


def test_failed_read_is_not_retried_within_ttl(monkeypatch):
    """A failing Firestore must be retried once per TTL, not per lookup.

    Regression: the cache previously refetched whenever no fetch had ever
    succeeded, so during an outage every single lookup paid a full request
    timeout. Three lookups on the auth path meant three timeouts per request.
    """
    for key in ("A", "B", "C"):
        monkeypatch.delenv(key, raising=False)
    client, firestore = _make_client(error=RuntimeError("firestore down"))
    assert client.get("A", default="a") == "a"
    assert client.get("B", default="b") == "b"
    assert client.get("C", default="c") == "c"
    assert firestore.reads == 1


def test_failed_read_retried_after_ttl(monkeypatch):
    """The outage backoff must still expire, so recovery is picked up."""
    monkeypatch.delenv("A", raising=False)
    firestore = _FakeFirestore(error=RuntimeError("firestore down"))
    client = ConfigClient(environment="testenv", cache_ttl=0.05, client_provider=lambda: firestore)
    client.get("A", default="a")
    time.sleep(0.06)
    client.get("A", default="a")
    assert firestore.reads == 2


# --- resolution source visibility -----------------------------------------


def test_explain_reports_env_source(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://local.example")
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    assert client.explain("AUTH_URL") == {
        "key": "AUTH_URL",
        "value": "https://local.example",
        "source": "env",
    }


def test_explain_reports_config_source(monkeypatch):
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    assert client.explain("AUTH_URL")["source"] == "config"


def test_explain_reports_default_source(monkeypatch):
    monkeypatch.delenv("NOWHERE", raising=False)
    client, _ = _make_client({})
    assert client.explain("NOWHERE", default="d") == {
        "key": "NOWHERE",
        "value": "d",
        "source": "default",
    }


def test_source_flips_when_envvar_removed(monkeypatch):
    """The canary signal: removing the ENVVAR must flip the source to config."""
    monkeypatch.setenv("AUTH_URL", "https://local.example")
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    assert client.get("AUTH_URL") == "https://local.example"
    assert client.sources()["AUTH_URL"] == "env"
    monkeypatch.delenv("AUTH_URL")
    assert client.get("AUTH_URL") == "https://remote.example"
    assert client.sources()["AUTH_URL"] == "config"


def test_source_change_is_logged(monkeypatch, caplog):
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    with caplog.at_level("INFO", logger="opteryx.config_client"):
        client.get("AUTH_URL")
        client.get("AUTH_URL")
        client.get("AUTH_URL")
    matching = [r for r in caplog.records if "AUTH_URL" in r.getMessage()]
    assert len(matching) == 1, "should log once per source change, not per lookup"
    assert "config" in matching[0].getMessage()


# --- environment detection ------------------------------------------------


def _reset_metadata_cache():
    opteryx_config.reset_metadata_cache()


def _clear_environment_vars(monkeypatch):
    for var in ("OPTERYX_ENVIRONMENT", "GCP_PROJECT_ID", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(var, raising=False)


def _metadata_response(body):
    response = mock.MagicMock()
    response.status = 200
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


def _fake_opener(response=None, error=None):
    opener = mock.Mock()
    if error is not None:
        opener.open.side_effect = error
    else:
        opener.open.return_value = response
    return opener


def test_environment_from_metadata_server(monkeypatch):
    """Cloud Run sets no project env var, so fall back to the metadata server."""
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()
    opener = _fake_opener(_metadata_response(b"opteryx-prod\n"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=opener):
        assert opteryx_config._detect_environment() == "opteryx-prod"
    _reset_metadata_cache()


def test_envvar_beats_metadata(monkeypatch):
    monkeypatch.setenv("OPTERYX_ENVIRONMENT", "from-env")
    _reset_metadata_cache()
    with mock.patch.object(opteryx_config.urllib.request, "build_opener") as build:
        assert opteryx_config._detect_environment() == "from-env"
        build.assert_not_called()
    _reset_metadata_cache()


def test_metadata_result_is_cached(monkeypatch):
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()
    opener = _fake_opener(_metadata_response(b"opteryx-prod"))
    with mock.patch.object(
        opteryx_config.urllib.request, "build_opener", return_value=opener
    ) as build:
        opteryx_config._detect_environment()
        opteryx_config._detect_environment()
        opteryx_config._detect_environment()
        assert build.call_count == 1
    _reset_metadata_cache()


def test_metadata_failure_is_not_retried_immediately(monkeypatch):
    """Off GCP the timeout must not be paid on every config read."""
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()
    opener = _fake_opener(error=OSError("no metadata server"))
    with mock.patch.object(
        opteryx_config.urllib.request, "build_opener", return_value=opener
    ) as build:
        assert opteryx_config._detect_environment() is None
        assert opteryx_config._detect_environment() is None
        assert build.call_count == 1
    _reset_metadata_cache()


def test_metadata_probe_is_bounded(monkeypatch):
    """The probe must stay sub-second.

    Regression: resolving the project through `google.auth.default()` probes the
    metadata server with a 3s timeout and three retries. Because the document is
    read at import, a host with no metadata and no ADC -- CI, a fresh laptop --
    blocked for around nine seconds before the process could start.
    """
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()
    opener = _fake_opener(_metadata_response(b"opteryx-prod"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=opener):
        opteryx_config._detect_environment()
    assert opener.open.call_args.kwargs["timeout"] <= 1.0
    _reset_metadata_cache()


def test_config_project_overrides_detection(monkeypatch):
    """The document can live in a project other than the one the service runs in."""
    monkeypatch.setenv("CONFIG_PROJECT", "opteryx-shared")
    client = ConfigClient(environment="testenv")
    assert client.project == "opteryx-shared"


# --- behaviour-change detection -------------------------------------------


def test_config_matching_default_does_not_change_behaviour(monkeypatch):
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://a.example"})
    result = client.compare("AUTH_URL", default="https://a.example")
    assert result["matches_default"] is True
    assert result["changes_behaviour"] is False


def test_config_differing_from_default_changes_behaviour(monkeypatch):
    """With no ENVVAR set, seeding a different value takes effect immediately."""
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://different.example"})
    result = client.compare("AUTH_URL", default="https://a.example")
    assert result["matches_default"] is False
    assert result["changes_behaviour"] is True


def test_envvar_shields_differing_config(monkeypatch):
    """An ENVVAR still wins, so config cannot change behaviour while it is set."""
    monkeypatch.setenv("AUTH_URL", "https://env.example")
    client, _ = _make_client({"AUTH_URL": "https://different.example"})
    result = client.compare("AUTH_URL", default="https://a.example")
    assert result["changes_behaviour"] is False
    assert result["safe_to_remove_envvar"] is False


# --- failure attribution ---------------------------------------------------


def test_read_failure_names_the_error(monkeypatch):
    monkeypatch.delenv("K", raising=False)
    client, _ = _make_client(error=PermissionError("Missing or insufficient permissions"))
    client.get("K", default="d")
    detail = client.status()["last_fetch_detail"]
    assert "PermissionError" in detail
    assert "insufficient permissions" in detail


def test_read_failure_logs_environment_and_project(monkeypatch, caplog):
    """A denied read is almost always the wrong project or a missing grant."""
    monkeypatch.delenv("K", raising=False)
    monkeypatch.setenv("CONFIG_PROJECT", "opteryx-prod")
    firestore = _FakeFirestore(error=PermissionError("denied"))
    client = ConfigClient(environment="mabeldev", client_provider=lambda: firestore)
    with caplog.at_level("WARNING", logger="opteryx.config_client"):
        client.get("K", default="d")
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "mabeldev" in msg
    assert "opteryx-prod" in msg
    assert "PermissionError" in msg


def test_successful_read_does_not_log_warning(caplog):
    client, _ = _make_client({"K": "v"})
    with caplog.at_level("WARNING", logger="opteryx.config_client"):
        client.get("K")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --- status ----------------------------------------------------------------


def test_status_reports_location(monkeypatch):
    monkeypatch.setenv("CONFIG_PROJECT", "opteryx-prod")
    client, _ = _make_client({"A": 1, "B": 2}, environment="mabeldev")
    status = client.status()
    assert status["environment"] == "mabeldev"
    assert status["project"] == "opteryx-prod"
    assert status["database"] == "(default)"
    assert status["collection"] == "config"
    assert status["keys_available"] == 2
    assert status["cache_ttl_seconds"] == 60.0


def test_status_contains_no_values():
    client, _ = _make_client({"SECRET_NAME": "very-identifiable"})
    assert "very-identifiable" not in repr(client.status())


# --- client injection ------------------------------------------------------


def test_injected_client_is_used_and_cached():
    """A service can hand over the Firestore client it already holds."""
    firestore = _FakeFirestore({"KEY": "v"})
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return firestore

    client = ConfigClient(environment="testenv", cache_ttl=0.0)
    client.set_client_provider(provider)
    client.get("KEY")
    client.get("KEY")
    assert calls["n"] == 1, "the provider should be called once, not per read"
    assert firestore.reads == 2


def test_metadata_failure_is_retried_after_the_window(monkeypatch):
    """A failed probe must expire. A successful one need not.

    The first probe happens at import with half a second of patience - the least
    forgiving moment in a container's life. Caching that failure for the life of
    the process meant one slow cold start left `environment` as None forever:
    every value silently fell back to an ENVVAR or a default, `refresh()` could
    not help, and only a new instance recovered.
    """
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()

    failing = _fake_opener(error=OSError("metadata server slow at cold start"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=failing):
        assert opteryx_config._detect_environment() is None

    # Move past the retry window rather than sleeping through it.
    monkeypatch.setattr(
        opteryx_config.time,
        "monotonic",
        lambda: opteryx_config._METADATA_FAILED_AT + opteryx_config._METADATA_RETRY_SECONDS + 1,
    )

    working = _fake_opener(_metadata_response(b"opteryx-prod"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=working):
        assert opteryx_config._detect_environment() == "opteryx-prod"

    _reset_metadata_cache()


def test_a_successful_metadata_probe_is_cached_indefinitely(monkeypatch):
    """The project genuinely never changes; only failures need to expire."""
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()

    opener = _fake_opener(_metadata_response(b"opteryx-prod"))
    with mock.patch.object(
        opteryx_config.urllib.request, "build_opener", return_value=opener
    ) as build:
        opteryx_config._detect_environment()
        monkeypatch.setattr(opteryx_config.time, "monotonic", lambda: 1e9)
        opteryx_config._detect_environment()
        assert build.call_count == 1

    _reset_metadata_cache()


def test_refresh_forgets_a_failed_metadata_probe(monkeypatch):
    """An undetected environment is a reason to call `refresh()` in the first place."""
    _clear_environment_vars(monkeypatch)
    _reset_metadata_cache()

    failing = _fake_opener(error=OSError("no metadata server"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=failing):
        assert opteryx_config._detect_environment() is None

    ConfigClient().refresh()

    working = _fake_opener(_metadata_response(b"opteryx-prod"))
    with mock.patch.object(opteryx_config.urllib.request, "build_opener", return_value=working):
        assert opteryx_config._detect_environment() == "opteryx-prod"

    _reset_metadata_cache()


# --- the first lookup, on a young container -------------------------------
#
# `_fetched_at` was 0.0 and the cache guard was
# `time.monotonic() - self._fetched_at >= self._cache_ttl`, on the stated
# assumption that "starts at zero, so the first lookup always fetches". Python
# documents `monotonic()`'s reference point as undefined, and a fresh container
# sandbox starts it near zero - so for the first CACHE_TTL_SECONDS of a
# container's life the subtraction stayed under the TTL and no read was ever
# attempted. The client served an empty document, logged nothing, and every
# lookup fell through to its default. `prime()` was a no-op for the same reason.
#
# Observed in production 2026-08-09: a revision deployed at 12:28:24 was still
# reporting `no fetch attempted` at 12:32:02, 218 seconds in.
#
# These pin the clock low, because at a realistic `monotonic()` the bug is
# invisible and every one of them passes against the broken code.


@pytest.fixture
def young_container(monkeypatch):
    """Set the monotonic clock to `n` seconds since a container that booted at 0."""

    def at(seconds):
        monkeypatch.setattr(opteryx_config.time, "monotonic", lambda: seconds)

    return at


@pytest.mark.parametrize("seconds", [0.0, 10.0, 54.0, 218.0, 299.0])
def test_the_first_lookup_fetches_however_young_the_container_is(young_container, seconds):
    young_container(seconds)
    client, firestore = _make_client({"KEY": "value"})

    assert client.get("KEY") == "value"
    assert firestore.reads == 1


def test_a_young_container_reports_the_read_it_made(young_container):
    """`no fetch attempted` surviving a status call is the tell."""
    young_container(54.0)
    client, _ = _make_client({"KEY": "value"})

    status = client.status()

    assert status["last_fetch_ok"] is True
    assert status["last_fetch_detail"] != "no fetch attempted"
    assert status["keys_available"] == 1


def test_the_cache_still_holds_within_the_ttl_on_a_young_container(young_container):
    """The fix must not turn every lookup into a read."""
    young_container(54.0)
    client, firestore = _make_client({"KEY": "value"})

    client.get("KEY")
    client.get("KEY")
    client.get("KEY")

    assert firestore.reads == 1


def test_prime_actually_reads_on_a_young_container(young_container):
    """`prime()` runs at import - the youngest the container ever is."""
    young_container(2.0)
    client, firestore = _make_client({"KEY": "value"})

    client.prime()

    assert firestore.reads == 1


def test_refresh_forces_a_reread_on_a_young_container(young_container):
    """`refresh()` reset to the same 0.0, so it could force nothing either."""
    young_container(54.0)
    client, firestore = _make_client({"KEY": "value"})
    client.get("KEY")
    assert firestore.reads == 1

    client.refresh()
    client.get("KEY")

    assert firestore.reads == 2


# --- promotion into os.environ --------------------------------------------


def test_promote_copies_config_into_environ(monkeypatch):
    monkeypatch.delenv("MAX_EXECUTION_WORKERS", raising=False)
    client, _ = _make_client({"MAX_EXECUTION_WORKERS": "8"})

    promoted = client.promote_to_environ()

    assert promoted == ["MAX_EXECUTION_WORKERS"]
    assert os.environ["MAX_EXECUTION_WORKERS"] == "8"


def test_promote_never_overwrites_an_existing_envvar(monkeypatch):
    """The whole hierarchy rests on this: a deployment's own ENVVAR wins."""
    monkeypatch.setenv("GCS_BUCKET", "set-on-the-service")
    client, _ = _make_client({"GCS_BUCKET": "from-the-document"})

    promoted = client.promote_to_environ()

    assert promoted == []
    assert os.environ["GCS_BUCKET"] == "set-on-the-service"


def test_promote_leaves_an_empty_envvar_alone(monkeypatch):
    """Set-but-empty is set. `if k not in os.environ` must not become truthiness."""
    monkeypatch.setenv("KVSTORE_LOCATION", "")
    client, _ = _make_client({"KVSTORE_LOCATION": "valkey://cache:6379"})

    client.promote_to_environ()

    assert os.environ["KVSTORE_LOCATION"] == ""


def test_promote_renders_booleans_lowercase(monkeypatch):
    """`str(True)` is `"True"`, which the `== "true"` idiom reads as false."""
    monkeypatch.delenv("DISABLE_OPTIMIZER", raising=False)
    monkeypatch.delenv("OPTERYX_DEBUG", raising=False)
    client, _ = _make_client({"DISABLE_OPTIMIZER": True, "OPTERYX_DEBUG": False})

    client.promote_to_environ()

    assert os.environ["DISABLE_OPTIMIZER"] == "true"
    assert os.environ["OPTERYX_DEBUG"] == "false"


def test_promote_renders_numbers_and_structures(monkeypatch):
    monkeypatch.delenv("PARQUET_GCS_IO_WORKERS", raising=False)
    monkeypatch.delenv("MATCH_THRESHOLD", raising=False)
    monkeypatch.delenv("KVSTORE_LAYERS", raising=False)
    client, _ = _make_client(
        {
            "PARQUET_GCS_IO_WORKERS": 16,
            "MATCH_THRESHOLD": 0.5,
            "KVSTORE_LAYERS": [{"name": "memory"}],
        }
    )

    client.promote_to_environ()

    assert os.environ["PARQUET_GCS_IO_WORKERS"] == "16"
    assert os.environ["MATCH_THRESHOLD"] == "0.5"
    assert json.loads(os.environ["KVSTORE_LAYERS"]) == [{"name": "memory"}]


def test_promote_skips_nulls_rather_than_writing_the_string_none(monkeypatch):
    monkeypatch.delenv("RESULTS_BUCKET", raising=False)
    client, _ = _make_client({"RESULTS_BUCKET": None})

    promoted = client.promote_to_environ()

    assert promoted == []
    assert "RESULTS_BUCKET" not in os.environ


def test_promote_survives_a_key_that_cannot_be_an_envvar_name(monkeypatch):
    """`os.environ[...] = ...` raises on these; at import that is the service down."""
    monkeypatch.delenv("GOOD_KEY", raising=False)
    client, _ = _make_client({"BAD=KEY": "x", "": "y", "GOOD_KEY": "z"})

    promoted = client.promote_to_environ()

    assert promoted == ["GOOD_KEY"]
    assert os.environ["GOOD_KEY"] == "z"


def test_promote_reports_the_source_as_promoted_not_env(monkeypatch):
    """Otherwise the migration log claims a deployment ENVVAR nobody set."""
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    client.promote_to_environ()

    assert client.explain("AUTH_URL")["source"] == "promoted"


def test_a_real_envvar_still_reports_env_after_promotion(monkeypatch):
    monkeypatch.setenv("AUTH_URL", "https://local.example")
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    client.promote_to_environ()

    assert client.explain("AUTH_URL")["source"] == "env"


def test_a_promoted_key_that_someone_else_overwrote_reports_env(monkeypatch):
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    client.promote_to_environ()

    os.environ["AUTH_URL"] = "https://changed.example"

    assert client.explain("AUTH_URL")["source"] == "env"


def test_compare_does_not_call_a_promoted_key_an_envvar(monkeypatch):
    """`safe_to_remove_envvar` about a variable no deployment sets is nonsense."""
    monkeypatch.delenv("AUTH_URL", raising=False)
    client, _ = _make_client({"AUTH_URL": "https://remote.example"})
    client.promote_to_environ()

    comparison = client.compare("AUTH_URL")

    assert comparison["in_env"] is False
    assert comparison["in_config"] is True
    assert comparison["safe_to_remove_envvar"] is False
    assert comparison["changes_behaviour"] is True


def test_promote_does_not_rewrite_its_own_value_on_a_second_call(monkeypatch):
    """A promoted value is bound by its consumer at import; rewriting only lies."""
    monkeypatch.delenv("MAX_EXECUTION_WORKERS", raising=False)
    client, firestore = _make_client({"MAX_EXECUTION_WORKERS": "8"})
    client.promote_to_environ()

    firestore._values = {"MAX_EXECUTION_WORKERS": "16"}
    client.refresh()

    assert client.promote_to_environ() == []
    assert os.environ["MAX_EXECUTION_WORKERS"] == "8"


def test_promote_picks_up_a_key_that_appears_later(monkeypatch):
    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    client, firestore = _make_client({"FIRST_KEY": "1"})
    client.promote_to_environ()

    firestore._values = {"FIRST_KEY": "1", "SECOND_KEY": "2"}
    client.refresh()

    assert client.promote_to_environ() == ["SECOND_KEY"]
    assert os.environ["SECOND_KEY"] == "2"


def test_promote_is_fail_open_when_firestore_is_unreachable(monkeypatch):
    """Import-time promotion must not take a service down with Firestore."""
    client, _ = _make_client({}, error=RuntimeError("firestore down"))

    assert client.promote_to_environ() == []


def test_status_counts_promoted_keys(monkeypatch):
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.setenv("B_KEY", "already-set")
    client, _ = _make_client({"A_KEY": "1", "B_KEY": "2"})
    client.promote_to_environ()

    assert client.status()["keys_promoted"] == 1


def test_a_promoted_project_cannot_redirect_the_document(monkeypatch):
    """The document must not be able to move the client to a different document.

    `GCP_PROJECT_ID` selects the document id and is also a value a service
    legitimately wants promoted (the worker builds its Firestore and GCS
    clients from it). Without the latch, promoting it means booting on one
    environment's configuration and switching to another at the next refresh.
    """
    monkeypatch.delenv("OPTERYX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "opteryx-prod")

    client = ConfigClient(
        client_provider=lambda: _FakeFirestore({"GCP_PROJECT_ID": "somewhere-else"})
    )
    assert client.environment == "opteryx-prod"

    monkeypatch.delenv("GCP_PROJECT_ID")  # so promotion is free to write it
    client.promote_to_environ()

    assert os.environ["GCP_PROJECT_ID"] == "somewhere-else"  # the service sees it
    assert client.environment == "opteryx-prod"  # the client does not follow it


def test_a_promoted_config_project_cannot_redirect_the_read(monkeypatch):
    monkeypatch.setenv("CONFIG_PROJECT", "config-holder")
    client, _ = _make_client({"CONFIG_PROJECT": "somewhere-else"})
    assert client.project == "config-holder"

    monkeypatch.delenv("CONFIG_PROJECT")
    client.promote_to_environ()

    assert os.environ["CONFIG_PROJECT"] == "somewhere-else"
    assert client.project == "config-holder"


def test_an_undetected_environment_is_not_latched(monkeypatch):
    """Only success latches -- a failed probe has to stay retryable."""
    for name in ("OPTERYX_ENVIRONMENT", "GCP_PROJECT_ID", "GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    client = ConfigClient(client_provider=lambda: _FakeFirestore({}))
    assert client.environment is None

    monkeypatch.setenv("OPTERYX_ENVIRONMENT", "arrived-late")

    assert client.environment == "arrived-late"
