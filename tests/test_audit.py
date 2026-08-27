"""Every request is audited once, after the response has really been sent.

The payload shape is a contract with xb500's `transform_audit_logs`, so the
field set is asserted directly. The truncation cases exist because
`BaseHTTPMiddleware.call_next()` returns as soon as status and headers are
captured -- seven of the eight implementations this replaces audited a response
that died mid-stream as a clean "200 okay".
"""

import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from opteryx_shared_services.audit import AuditMiddleware
from opteryx_shared_services.audit import unverified_claims

CORE_FIELDS = {"method", "path", "status", "duration_ms", "from", "timestamp"}


@pytest.fixture
def audited(monkeypatch):
    """Build a client over an app wrapped in the middleware, capturing records."""
    records = []

    def _build(handler, path="/thing", extra_fields=None, middleware_state=None):
        from opteryx_shared_services import audit as audit_module

        class _Recorder:
            def audit(self, payload):
                records.append(payload)

            def warning(self, message):
                records.append({"_warning": message})

        monkeypatch.setattr(audit_module, "logger", _Recorder())

        app = Starlette(routes=[Route(path, handler, methods=["GET", "POST"])])
        wrapped = AuditMiddleware(app, extra_fields=extra_fields)
        return TestClient(wrapped, raise_server_exceptions=False), records

    return _build


def _ok(request):
    return PlainTextResponse("fine")


# --- the payload contract ---------------------------------------------------


def test_the_core_fields_are_always_present(audited):
    client, records = audited(_ok)

    client.get("/thing")

    assert CORE_FIELDS <= set(records[0])


def test_the_payload_is_the_agreed_shape(audited):
    client, records = audited(_ok)

    client.get("/thing")

    assert set(records[0]) == CORE_FIELDS | {"detail", "client_ip", "user_agent", "host"}


def test_a_clean_response_is_recorded_as_okay(audited):
    client, records = audited(_ok)

    client.get("/thing")

    assert records[0]["status"] == 200
    assert records[0]["detail"] == "okay"
    assert records[0]["path"] == "/thing"
    assert records[0]["method"] == "GET"


def test_the_proxy_header_and_the_real_peer_are_both_recorded(audited):
    """`from` is what a proxy claims; `client_ip` is the peer the server saw.
    They differ exactly when it matters."""
    client, records = audited(_ok)

    client.get("/thing", headers={"x-forwarded-for": "203.0.113.9"})

    assert records[0]["from"] == "203.0.113.9"
    assert records[0]["client_ip"] != "203.0.113.9"


def test_one_record_per_request(audited):
    client, records = audited(_ok)

    client.get("/thing")
    client.get("/thing")

    assert len([r for r in records if "_warning" not in r]) == 2


# --- what BaseHTTPMiddleware could not see ----------------------------------


def test_a_response_that_dies_mid_stream_is_not_recorded_as_a_clean_200(audited):
    """The defect in seven of the eight implementations this replaces."""

    async def body_then_boom(request):
        async def stream():
            yield b"partial"
            raise RuntimeError("the compressor died")

        return StreamingResponse(stream())

    client, records = audited(body_then_boom)

    client.get("/thing")

    record = records[0]
    assert record["detail"] != "okay"
    assert "transport failure after 7 bytes sent" in record["detail"]
    assert "the compressor died" in record["detail"]


def test_a_handled_exception_reports_the_exception_not_a_truncation(audited):
    """Starlette's ServerErrorMiddleware renders a complete 500 and re-raises,
    so an exception reaching us does NOT imply the client got a broken response.
    Reporting that as a transport failure would misdescribe a client that
    received a whole, correct error page."""

    def boom(request):
        raise RuntimeError("never started")

    client, records = audited(boom)

    client.get("/thing")

    assert records[0]["status"] == 500
    assert records[0]["detail"] == "RuntimeError: never started"
    assert "transport failure" not in records[0]["detail"]


def test_the_exception_is_re_raised_not_swallowed(audited):
    """Swallowing it into a synthetic empty Response would deny Starlette's
    ServerErrorMiddleware the chance to produce a real error response."""

    def boom(request):
        raise RuntimeError("never started")

    client, _ = audited(boom)

    # `raise_server_exceptions=False` means the test client renders the 500 the
    # error middleware produced -- which only exists because we re-raised.
    assert client.get("/thing").status_code == 500


# --- identity ---------------------------------------------------------------


def _bearer(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"Bearer header.{body}.signature"


def test_the_subject_is_recorded_from_an_unverified_token(audited):
    """Audit must not require a key fetch; the claims only attribute the
    request, they never make an access decision."""
    client, records = audited(_ok)

    client.get("/thing", headers={"authorization": _bearer({"sub": "user-42"})})

    assert records[0]["jwt_present"] is True
    assert records[0]["jwt_sub"] == "user-42"


def test_verified_claims_on_request_state_are_preferred(audited):
    """An auth dependency has already decoded the token; a second decode per
    request is waste."""

    async def handler(request):
        request.state.claims = {"sub": "verified-user"}
        return PlainTextResponse("fine")

    client, records = audited(handler)

    client.get("/thing", headers={"authorization": _bearer({"sub": "unverified-user"})})

    assert records[0]["jwt_sub"] == "verified-user"


def test_no_token_means_no_identity_fields(audited):
    client, records = audited(_ok)

    client.get("/thing")

    assert "jwt_present" not in records[0]
    assert "jwt_sub" not in records[0]


def test_a_malformed_token_does_not_break_the_record(audited):
    client, records = audited(_ok)

    client.get("/thing", headers={"authorization": "Bearer not-a-jwt"})

    assert records[0]["jwt_present"] is True
    assert "jwt_sub" not in records[0]


@pytest.mark.parametrize("token", ["", "a", "a.b", "a.!!!.c", "a." + "x" * 5 + ".c"])
def test_unverified_claims_never_raises(token):
    assert unverified_claims(token) is None or isinstance(unverified_claims(token), dict)


# --- extension points -------------------------------------------------------


def test_a_handler_can_add_fields_via_request_state(audited):
    """How a route reports what it did, without the middleware knowing the route."""

    async def handler(request):
        request.state.audit_extra = {"rows_written": 12}
        return PlainTextResponse("fine")

    client, records = audited(handler)

    client.get("/thing")

    assert records[0]["rows_written"] == 12


def test_extra_fields_may_add_but_never_overwrite(audited):
    """An extension must not rewrite the fields the ingestion selects on."""
    client, records = audited(
        _ok, extra_fields=lambda request: {"status": 999, "www_authenticate": "Bearer"}
    )

    client.get("/thing")

    assert records[0]["status"] == 200
    assert records[0]["www_authenticate"] == "Bearer"


def test_a_raising_extension_does_not_lose_the_record(audited):
    """An incomplete audit record beats no record, and beats a 500 caused by
    the auditing itself."""

    def broken(request):
        raise RuntimeError("bad hook")

    client, records = audited(_ok, extra_fields=broken)

    response = client.get("/thing")

    assert response.status_code == 200
    assert [r for r in records if "_warning" not in r][0]["detail"] == "okay"


def test_non_http_traffic_is_passed_through_unaudited(audited):
    """Lifespan and websocket scopes are not requests."""
    client, records = audited(_ok)

    with client:  # runs the lifespan
        client.get("/thing")

    assert len([r for r in records if "_warning" not in r]) == 1
