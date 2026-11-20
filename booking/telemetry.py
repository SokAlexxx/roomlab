"""Structured request telemetry (KAN-13 / B3).

One JSON line per request on the ``roomlab.request`` logger with exactly:
time (UTC, ISO 8601), operation, status, elapsed_ms, request_id.

Deliberately not logged: headers (Authorization), query strings, bodies,
usernames and passwords. ``operation`` is the method plus the URL route name,
not the raw path. A client ``X-Request-ID`` is reused only if it matches a
strict pattern; otherwise a fresh UUID is generated. The id is echoed back in
the ``X-Request-ID`` response header so a client can correlate a log line.
"""
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone

from django.urls import Resolver404, resolve

logger = logging.getLogger("roomlab.request")
REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _configure_default_handler():
    # Django's default LOGGING has no handler for custom loggers, so INFO lines
    # would be dropped. Attach a plain stderr handler for the running service;
    # the test runner is skipped to keep test output readable (tests capture
    # the logger with assertLogs instead).
    if logger.handlers or (len(sys.argv) > 1 and sys.argv[1] == "test"):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_default_handler()


def request_id_for(request):
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    if _SAFE_ID.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


def operation_for(request):
    try:
        name = resolve(request.path_info).url_name or "unnamed"
    except Resolver404:
        name = "unresolved"
    return f"{request.method} {name}"


def emit(operation, status, elapsed_ms, request_id):
    line = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "operation": operation,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "request_id": request_id,
    }
    logger.info(json.dumps(line, ensure_ascii=True, separators=(",", ":")))


class RequestTelemetry:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request_id_for(request)
        request.request_id = request_id
        operation = operation_for(request)
        started = time.perf_counter()
        status = 500
        try:
            response = self.get_response(request)
            status = response.status_code
        finally:
            # Also logged (as 500) when the view raises; the exception propagates.
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            emit(operation, status, elapsed_ms, request_id)
        response[REQUEST_ID_HEADER] = request_id
        return response
