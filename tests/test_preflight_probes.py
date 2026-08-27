"""Each probe must exercise the permission the service actually uses.

Not a proxy for it. A bucket that is read is probed with a LIST (because
`objectViewer` cannot GET the bucket); a bucket that is only written is probed
with a CREATE (because `objectCreator` cannot read it back). Getting this wrong
grounds a correctly-configured service, or -- worse -- passes one whose grants
fail at the first real request.
"""

from opteryx_shared_services import preflight


# --- storage ----------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name="unset"):
        self.name = name
        self.uploaded = None
        self.deleted = False
        self.delete_raises = None

    def upload_from_string(self, data, **kwargs):
        self.uploaded = kwargs

    def delete(self, **kwargs):
        if self.delete_raises:
            raise self.delete_raises
        self.deleted = True


class _FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name):
        self._blob.name = name
        return self._blob


class _FakeStorage:
    def __init__(self, blob=None, listed=()):
        self._blob = blob
        self._listed = listed
        self.list_args = None

    def bucket(self, name):
        return _FakeBucket(self._blob)

    def list_blobs(self, bucket, **kwargs):
        self.list_args = (bucket, kwargs)
        return iter(self._listed)


def test_missing_bucket_configuration_fails_rather_than_being_discovered_mid_request():
    assert not preflight.gcs_list("gcs-data", bucket_key="GCS_BUCKET")().ok
    assert not preflight.gcs_write("gcs-results", bucket_key="RESULTS_BUCKET")().ok


def test_a_read_bucket_is_probed_with_a_list(config, monkeypatch):
    """`objectViewer` can list objects but cannot GET the bucket."""
    config(GCS_BUCKET="opteryx-data")
    client = _FakeStorage(listed=["an-object"])
    monkeypatch.setattr(preflight, "_storage_client", lambda: client)

    result = preflight.gcs_list("gcs-data", bucket_key="GCS_BUCKET")()

    assert result.ok
    assert client.list_args[0] == "opteryx-data"
    assert client.list_args[1]["max_results"] == 1


def test_a_write_bucket_is_probed_with_a_create_and_cleans_up(config, monkeypatch):
    """The service only ever CREATEs here, so that is what gets proved."""
    config(RESULTS_BUCKET="opteryx-results")
    blob = _FakeBlob()
    monkeypatch.setattr(preflight, "_storage_client", lambda: _FakeStorage(blob=blob))

    result = preflight.gcs_write("gcs-results", bucket_key="RESULTS_BUCKET")()

    assert result.ok
    assert blob.name.startswith(preflight.PROBE_PREFIX)
    # A create, not an overwrite -- `objectCreator` alone must pass.
    assert blob.uploaded["if_generation_match"] == 0
    assert blob.deleted


def test_the_write_probe_survives_a_denied_delete(config, monkeypatch):
    """`objectCreator` can write the probe object but not remove it; the service
    can still serve every request, so this is a warning, not a failure."""
    config(RESULTS_BUCKET="opteryx-results")
    blob = _FakeBlob()
    blob.delete_raises = RuntimeError("403 delete denied")
    monkeypatch.setattr(preflight, "_storage_client", lambda: _FakeStorage(blob=blob))

    assert preflight.gcs_write("gcs-results", bucket_key="RESULTS_BUCKET")().ok


# --- firestore --------------------------------------------------------------


class _FakeFirestore:
    def __init__(self):
        self.read = []

    def collection(self, name):
        outer = self

        class _Collection:
            def document(self, doc):
                class _Doc:
                    def get(self, **kwargs):
                        outer.read.append((name, doc, kwargs))

                return _Doc()

        return _Collection()


def test_a_collection_is_probed_with_a_read(monkeypatch):
    client = _FakeFirestore()
    monkeypatch.setattr(preflight, "_firestore_client_for", lambda database: client)

    result = preflight.firestore_read("firestore-jobs", collection="jobs")()

    assert result.ok
    collection, document, kwargs = client.read[0]
    assert (collection, document) == ("jobs", preflight.PROBE_DOCUMENT)
    assert kwargs["timeout"] == preflight.PROBE_TIMEOUT_SECONDS


def test_a_second_database_check_defers_when_it_resolves_to_the_default(monkeypatch):
    """FIRESTORE_DATABASE unset -> `(default)`, which another check already reads."""
    monkeypatch.setattr(preflight, "_firestore_client_for", lambda database: _FakeFirestore())

    result = preflight.firestore_read(
        "firestore-catalog",
        collection="$catalog",
        database_key="FIRESTORE_DATABASE",
        covered_by="firestore-jobs",
    )()

    assert result.ok
    assert "firestore-jobs" in result.detail


def test_a_second_database_is_probed_when_it_differs(config, monkeypatch):
    config(FIRESTORE_DATABASE="catalog")
    asked = []
    monkeypatch.setattr(
        preflight,
        "_firestore_client_for",
        lambda database: asked.append(database) or _FakeFirestore(),
    )

    result = preflight.firestore_read(
        "firestore-catalog",
        collection="$catalog",
        database_key="FIRESTORE_DATABASE",
        covered_by="firestore-jobs",
    )()

    assert result.ok
    assert asked == ["catalog"]


def test_the_database_name_is_resolved_when_the_probe_runs_not_at_import(config, monkeypatch):
    """Config is promoted into the environment during startup, so a name bound
    at import can be older than the one the rest of the process is using."""
    asked = []
    monkeypatch.setattr(
        preflight,
        "_firestore_client_for",
        lambda database: asked.append(database) or _FakeFirestore(),
    )
    probe = preflight.firestore_read(
        "firestore-catalog", collection="$catalog", database_key="FIRESTORE_DATABASE"
    )

    config(FIRESTORE_DATABASE="catalog")
    probe()

    assert asked == ["catalog"]


# --- secret manager ---------------------------------------------------------


def test_a_list_denial_is_not_a_failure(monkeypatch):
    """Least privilege here is per-secret `secretAccessor`, which carries no
    `secrets.list` -- failing on that would ground a healthy service."""
    from google.api_core import exceptions as api_exceptions

    monkeypatch.setattr(preflight, "_project", lambda: "opteryx-prod")

    class _Client:
        def list_secrets(self, request, timeout=None):
            raise api_exceptions.PermissionDenied("no secrets.list")

    monkeypatch.setattr(preflight, "_secret_manager_client", lambda: _Client())

    result = preflight.secret_manager()()

    assert result.ok
    assert "cannot list" in result.detail


def test_a_named_secret_is_read_when_configured(config, monkeypatch):
    config(HEALTH_CHECK_SECRET="GITHUB_TOKEN")
    monkeypatch.setattr(preflight, "_project", lambda: "opteryx-prod")
    requested = {}

    class _Payload:
        data = b"a-token"

    class _Response:
        payload = _Payload()

    class _Client:
        def access_secret_version(self, request, timeout=None):
            requested.update(request)
            return _Response()

    monkeypatch.setattr(preflight, "_secret_manager_client", lambda: _Client())

    result = preflight.secret_manager()()

    assert result.ok
    assert requested["name"] == "projects/opteryx-prod/secrets/GITHUB_TOKEN/versions/latest"


def test_without_a_project_the_secret_check_fails(monkeypatch):
    monkeypatch.setattr(preflight, "_project", lambda: None)

    assert not preflight.secret_manager()().ok
