"""Request audit logging, and the payload shape the fleet's ingestion reads.

Every service audits every request. The record goes out through
`logger.audit(...)` as a raw JSON stdout line, and `xb500.opteryx`'s
`transform_audit_logs` ingests it into `ops.audit_log` -- so the payload shape
is a cross-service contract in the same way the wire format is. See
`opteryx_shared_services.logging` for the wire format itself.

THE PAYLOAD
-----------
Ten fields, the same for every service::

    method, path, status, duration_ms, from, timestamp   -- the core
    detail                                               -- why, when not "okay"
    jwt_present, jwt_sub                                 -- who
    client_ip, user_agent, host                          -- from where, really

`from` is the `x-forwarded-for` header, which is what a proxy claims; `client_ip`
is the peer the server actually saw. They differ exactly when it matters.

Anything else a service needs goes through the extension points below rather
than into a fork of this file: the eight implementations this replaces had
diverged into eight payload shapes, of which only the first six fields were
common.

WHY PURE ASGI, NOT BaseHTTPMiddleware
-------------------------------------
`BaseHTTPMiddleware.call_next()` returns a Response as soon as the status and
headers are captured. It has no visibility into whether the body actually
finished streaming to the client. So a response that started as a clean 200 and
then died mid-stream -- a compression middleware crash, a worker OOM, a dropped
connection -- was audited as a plain "200 okay", indistinguishable from one that
fully succeeded, because the record was written before the body was ever sent.
Seven of the eight services this replaces had that defect.

This wraps `send` directly instead, tracking the bytes actually written and
whether the stream was cleanly terminated (a final message with
`more_body=False`), and reports a distinct status and detail when it was not.
That is the signal a "200 but the client got nothing" incident needs.

It also does not swallow exceptions into a synthetic empty Response: anything
that reaches here is recorded and then RE-RAISED, so Starlette's own
`ServerErrorMiddleware` (which wraps this entire stack) turns it into a real
error response instead of a blank one going out to the client.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional

from starlette.requests import Request

from opteryx_shared_services.logging import get_logger

logger = get_logger()

__all__ = ["AuditMiddleware", "unverified_claims"]


def unverified_claims(token: str) -> Optional[Dict[str, Any]]:
    """Decode a JWT's claims WITHOUT verifying the signature.

    Audit must not require a key fetch -- a JWKS round trip on the audit path
    would put an outbound HTTP call in every request's teardown, and a failure
    there would lose the record entirely. The claims are used only to attribute
    the request in the log, never to make an access decision, and by the time
    this runs the request has already been allowed or refused on a verified
    path.

    Decoded here rather than with `python-jose`, which xb500 does not depend on:
    the claims segment is just base64url JSON, so reading it needs no library
    and no dependency this package would otherwise not have.
    """
    try:
        segment = token.split(".")[1]
        # base64url without padding; `binascii` rejects the wrong amount, so
        # pad it back up to a multiple of four.
        padded = segment + "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


class AuditMiddleware:
    """Audit every HTTP request, once, after the response has really been sent.

    Added like any other middleware::

        service.add_middleware(AuditMiddleware)

    Two extension points, so a service can record something specific without
    forking the payload:

    - ``request.state.audit_extra`` -- a dict any route may set while handling
      the request. Merged into the payload. This is how a handler reports what
      it did (the result of the work, a failure reason) without the middleware
      having to know anything about that route.
    - ``extra_fields`` -- a callable taking the `Request` and returning a dict,
      run after the response completes. For things derived from the request or
      the environment rather than from the handler.

    Neither may overwrite a standard field, and a raising `extra_fields` is
    logged and ignored: an audit record that is merely incomplete is better than
    no audit record, and far better than a 500 caused by the auditing itself.
    """

    def __init__(self, app, extra_fields: Optional[Callable[[Request], Dict[str, Any]]] = None):
        self.app = app
        self.extra_fields = extra_fields

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        start = time.time()
        state = {"status": None, "bytes_sent": 0, "body_started": False, "completed": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["body_started"] = True
            elif message["type"] == "http.response.body":
                state["bytes_sent"] += len(message.get("body", b""))
                if not message.get("more_body", False):
                    state["completed"] = True
            await send(message)

        transport_error = None
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            transport_error = exc
        finally:
            self._record(request, scope, state, transport_error, start)

        if transport_error is not None:
            # Re-raised, NOT swallowed into a synthetic empty Response, so
            # Starlette's ServerErrorMiddleware can produce a real error.
            raise transport_error

    # -- the record --------------------------------------------------------

    @staticmethod
    def _status_and_detail(state, transport_error) -> tuple:
        """What happened, and how the client experienced it."""
        if transport_error is not None:
            status = state["status"] or getattr(transport_error, "status_code", 500)
            if state["body_started"] and not state["completed"]:
                # Headers (possibly a 2xx) already went out before the chain
                # died, and the body never finished -- the client received a
                # truncated response despite the "successful" status. This is
                # the case BaseHTTPMiddleware cannot see.
                return status, (
                    f"transport failure after {state['bytes_sent']} bytes sent: "
                    f"{type(transport_error).__name__}: {transport_error}"
                )
            # Either nothing went out at all, or a complete response did before
            # the exception propagated -- which is the normal path when
            # Starlette's ServerErrorMiddleware renders a 500 and re-raises.
            # Calling that a transport failure would misreport a client that
            # got a whole, correct error response.
            return status, f"{type(transport_error).__name__}: {transport_error}"

        if state["body_started"] and not state["completed"]:
            # No exception, but the stream was never cleanly terminated: the app
            # returned without a final more_body=False chunk.
            return state["status"] or 0, (
                f"incomplete response: only {state['bytes_sent']} bytes sent, stream not terminated"
            )

        return state["status"] or 0, "okay"

    def _record(self, request: Request, scope, state, transport_error, start) -> None:
        duration_ms = int((time.time() - start) * 1000)
        status, detail = self._status_and_detail(state, transport_error)

        payload: Dict[str, Any] = {
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": duration_ms,
            # What the proxy claims...
            "from": request.headers.get("x-forwarded-for", "-"),
            # ...and the peer the server actually saw.
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent", ""),
            "host": request.headers.get("host", ""),
            "timestamp": int(time.time()),
            "detail": detail,
        }
        payload.update(self._identity(request, scope))

        for extra in (self._state_extra(scope), self._callable_extra(request)):
            for key, value in extra.items():
                # `setdefault`: an extension may add fields, never rewrite the
                # ones the ingestion selects and joins on.
                payload.setdefault(key, value)

        logger.audit(payload)

    @staticmethod
    def _identity(request: Request, scope) -> Dict[str, Any]:
        """Who made the request, if a bearer token says so.

        Prefers claims an auth dependency has already verified and stashed on
        `request.state` -- that is backed by the ASGI scope and so is visible
        here once the app has run, which avoids a second decode per request.
        Falls back to an unverified read only when the request never reached
        that dependency (refused upstream, no matching route), where there is
        nothing on state to read.
        """
        auth = request.headers.get("authorization")
        if not auth or not auth.lower().startswith("bearer "):
            return {}

        identity: Dict[str, Any] = {"jwt_present": True}
        claims = scope.get("state", {}).get("claims")
        if not isinstance(claims, dict):
            claims = unverified_claims(auth.split(" ", 1)[1])
        if isinstance(claims, dict) and claims.get("sub"):
            identity["jwt_sub"] = claims["sub"]
        return identity

    @staticmethod
    def _state_extra(scope) -> Dict[str, Any]:
        extra = scope.get("state", {}).get("audit_extra")
        return extra if isinstance(extra, dict) else {}

    def _callable_extra(self, request: Request) -> Dict[str, Any]:
        if self.extra_fields is None:
            return {}
        try:
            extra = self.extra_fields(request)
        except Exception as exc:
            logger.warning(f"audit extra_fields failed: {type(exc).__name__}: {exc}")
            return {}
        return extra if isinstance(extra, dict) else {}
