"""The audit wire format is a contract with xb500's log ingestion.

`transform_audit_logs` selects Cloud Logging entries whose
`jsonPayload.severity` is "AUDIT"; `transform_billing_logs` selects "BILLING".
Cloud Logging only parses a stdout line into `jsonPayload` when the whole line
is valid JSON. Every assertion about audit records below exists because one of
the eight per-service implementations this module replaces broke one of those
three requirements and nothing noticed.
"""

import io
import json
import logging

import pytest

from opteryx_shared_services import logging as slogging


@pytest.fixture
def logger_streams(monkeypatch):
    """A configured logger whose two handlers write into buffers we can read.

    Built through `get_logger` rather than by hand, so the handler split, the
    filters and the formatter choice are the real ones.
    """

    def _build(name, **environ):
        for key in ("K_SERVICE", "LOG_FORMAT", "LOGGING_LEVEL"):
            monkeypatch.delenv(key, raising=False)
        for key, value in environ.items():
            monkeypatch.setenv(key, value)

        logging.getLogger(name).handlers.clear()
        logger = slogging.get_logger(name)
        ordinary, audit = io.StringIO(), io.StringIO()
        # The handlers are ordered as `get_logger` added them: ordinary first.
        logger.handlers[0].stream = ordinary
        logger.handlers[1].stream = audit
        return logger, ordinary, audit

    yield _build
    slogging.set_trace_context(None)


# --- the audit contract -----------------------------------------------------


def test_an_audit_record_is_valid_json_on_its_own(logger_streams):
    """A `time | LEVEL | name | {...}` prefix makes the line opaque textPayload,
    which is how jobs, upload and authenticate's audit records became invisible."""
    logger, ordinary, audit = logger_streams("t.audit.json")

    logger.audit({"event": "query", "rows": 3})

    line = audit.getvalue().strip()
    assert json.loads(line) == {"severity": "AUDIT", "event": "query", "rows": 3}
    assert ordinary.getvalue() == ""


def test_an_audit_record_carries_its_own_severity(logger_streams):
    """The LogEntry's GCP-assigned severity is not what the transform selects on."""
    logger, _, audit = logger_streams("t.audit.severity")

    logger.audit({"event": "query"})

    assert json.loads(audit.getvalue())["severity"] == "AUDIT"


def test_a_caller_supplied_severity_survives_verbatim(logger_streams):
    """Billing events pass severity="BILLING" and are ingested by a different
    transform -- stamping "AUDIT" over it would route them to the wrong table."""
    logger, _, audit = logger_streams("t.audit.billing")

    logger.audit({"severity": "BILLING", "workspace": "acme", "bytes": 1024})

    assert json.loads(audit.getvalue())["severity"] == "BILLING"


def test_the_audit_level_is_never_mapped_to_a_cloud_logging_severity():
    """worker.opteryx mapped AUDIT -> "NOTICE" in its structured formatter, so
    its audit records could never match a `severity == "AUDIT"` selector."""
    assert slogging.AUDIT not in slogging._SEVERITY_BY_LEVEL
    assert slogging.ALERT not in slogging._SEVERITY_BY_LEVEL


def test_audit_records_are_raw_json_in_text_mode_too(logger_streams):
    """Audit records are machine-read in local development as well, so the
    output mode must not change their shape."""
    logger, _, audit = logger_streams("t.audit.textmode")  # no K_SERVICE -> text mode

    logger.audit({"event": "query"})

    assert json.loads(audit.getvalue())["event"] == "query"


def test_an_audit_payload_is_not_nested_by_the_formatter(logger_streams):
    """Re-wrapping an already-serialised payload would push every field one
    level deeper and break every downstream selector."""
    logger, _, audit = logger_streams("t.audit.flat")

    logger.audit({"event": "query"})
    payload = json.loads(audit.getvalue())

    assert "message" not in payload
    assert payload["event"] == "query"


def test_alert_records_share_the_audit_channel(logger_streams):
    """odata's logging registered an ALERT level; it needs the same treatment,
    not the text handler."""
    logger, ordinary, audit = logger_streams("t.alert")

    logger.alert({"event": "sink-failed"})

    assert json.loads(audit.getvalue())["severity"] == "ALERT"
    assert ordinary.getvalue() == ""


# --- ordinary records -------------------------------------------------------


def test_ordinary_records_are_json_with_a_mapped_severity_on_cloud_run(logger_streams):
    """Formatted text lands as textPayload at DEFAULT severity, so severity-based
    alerting cannot see even an ERROR."""
    logger, ordinary, _ = logger_streams("t.ord.json", K_SERVICE="svc")

    logger.error("it broke")

    payload = json.loads(ordinary.getvalue())
    assert payload["severity"] == "ERROR"
    assert payload["message"] == "it broke"
    assert payload["logger"] == "t.ord.json"


def test_ordinary_records_are_human_readable_off_cloud_run(logger_streams):
    logger, ordinary, _ = logger_streams("t.ord.text")

    logger.info("hello")

    assert "| INFO     | t.ord.text | hello" in ordinary.getvalue()


def test_log_format_overrides_the_mode_in_both_directions(logger_streams):
    logger, ordinary, _ = logger_streams("t.ord.forced", LOG_FORMAT="json")
    logger.info("forced json")
    assert json.loads(ordinary.getvalue())["message"] == "forced json"

    logger, ordinary, _ = logger_streams("t.ord.forcedtext", K_SERVICE="svc", LOG_FORMAT="text")
    logger.info("forced text")
    assert "| INFO     |" in ordinary.getvalue()


def test_a_traceback_is_part_of_the_message(logger_streams):
    """Error Reporting groups an exception only when the traceback is in
    `message`, not a sibling field."""
    logger, ordinary, _ = logger_streams("t.ord.exc", K_SERVICE="svc")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("while serving")

    payload = json.loads(ordinary.getvalue())
    assert payload["message"].startswith("while serving\n")
    assert "ValueError: boom" in payload["message"]


def test_extra_fields_ride_along_as_their_own_json_fields(logger_streams):
    logger, ordinary, _ = logger_streams("t.ord.extra", K_SERVICE="svc")

    logger.info("served", extra={"workspace": "acme"})

    assert json.loads(ordinary.getvalue())["workspace"] == "acme"


# --- trace correlation ------------------------------------------------------


def test_the_request_trace_reaches_both_channels(logger_streams):
    logger, ordinary, audit = logger_streams("t.trace", K_SERVICE="svc")
    slogging.set_trace_context("projects/p/traces/abc")

    logger.info("served")
    logger.audit({"event": "query"})

    assert json.loads(ordinary.getvalue())["logging.googleapis.com/trace"].endswith("abc")
    assert json.loads(audit.getvalue())["logging.googleapis.com/trace"].endswith("abc")


def test_a_non_json_audit_message_is_passed_through_untouched(logger_streams):
    """`logger.audit("a string")` must not become malformed JSON on the wire."""
    logger, _, audit = logger_streams("t.trace.plain")
    slogging.set_trace_context("projects/p/traces/abc")

    logger.audit("not a dict")

    assert audit.getvalue().strip() == "not a dict"


# --- wiring -----------------------------------------------------------------


def test_records_do_not_propagate_to_the_root_logger(logger_streams):
    """uvicorn's default configuration would otherwise duplicate every line."""
    logger, _, _ = logger_streams("t.wiring.propagate")

    assert logger.propagate is False


def test_third_party_warnings_are_adopted_rather_than_lost(logger_streams):
    """Without a handler these inherit the unconfigured root logger, so a
    "sink GitHubSink failed" warning goes out unformatted and easy to miss."""
    for name in slogging._ADOPTED_LOGGERS:
        logging.getLogger(name).handlers.clear()

    logger_streams("t.wiring.adopt")

    for name in slogging._ADOPTED_LOGGERS:
        adopted = logging.getLogger(name)
        assert adopted.handlers
        assert adopted.level == logging.WARNING


def test_reconfiguring_does_not_reset_a_level_a_caller_has_set(logger_streams):
    """Callers adjust the level after the first `get_logger()` and expect it to
    stick -- worker.opteryx sets it to 5."""
    logger, _, _ = logger_streams("t.wiring.level")
    logger.setLevel(5)

    assert slogging.get_logger("t.wiring.level").level == 5
