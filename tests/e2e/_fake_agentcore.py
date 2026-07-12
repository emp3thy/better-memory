"""Local fake AWS Bedrock AgentCore endpoint for the hermetic T2 suite.

A ``ThreadingHTTPServer`` bound to ``127.0.0.1`` on an ephemeral port. The
operation routing table is derived from the *installed* botocore service
models (``botocore/data/bedrock-agentcore*/**/service-2.json.gz``, gzip
decoded — the models ship gzipped at boto3/botocore 1.43.x), so path
matching is pinned to the real wire contract, never guessed.

Behavior:

* every request is recorded as a :class:`RecordedRequest`
  ``(operation, method, path, headers, body)`` — synchronously, before the
  response is written, so tests can assert immediately after the client
  call returns;
* responses are canned per operation name via :meth:`FakeAgentCore.
  set_response` (a dict, or a callable ``RecordedRequest -> dict`` for
  per-request behavior);
* any un-canned (or unroutable) request gets a ``200 {}`` fallback —
  botocore's rest-json parser tolerates missing output members, and the
  repo's backend code uses ``response.get(...)`` throughout;
* the live-verified record-plane dialect is ENFORCED before any canned
  response is consulted (see :func:`dialect_violation`): unknown
  BatchCreate/BatchUpdate record keys and non-indexed metadata-filter keys
  come back as HTTP 400 ValidationException, and BatchUpdate metadata
  violations (reserved ``x-amz-agentcore-memory-*`` keys, a non-STRING
  ``last_credited_at``) come back the way real AWS reports them — HTTP 200
  with ``failedRecords`` — so the hermetic tier cannot lie about shapes
  real AWS rejects.

Plain HTTP, no TLS — verified end-to-end against boto3 1.43.14 (the
``AWS_ENDPOINT_URL`` seam covers both the ``bedrock-agentcore`` data plane
and the ``bedrock-agentcore-control`` control plane).

Usage::

    with FakeAgentCore() as fake:
        fake.set_response("ListMemoryRecords", {"memoryRecordSummaries": []})
        env = agentcore_env(clean_slate_home, fake.port)
        ...
        assert len(fake.requests_for("ListMemoryRecords")) == 3
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import botocore

#: Both planes' models, relative to the botocore package dir. Models are
#: gzipped at botocore 1.43.14 (judge fix), but the packaging has flipped
#: between .json and .json.gz across botocore history — glob both so a
#: botocore upgrade cannot silently break routing (review fix).
_MODEL_GLOBS = (
    "data/bedrock-agentcore*/*/service-2.json.gz",
    "data/bedrock-agentcore*/*/service-2.json",
)

_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")

# ---------------------------------------------------------------------------
# Live-verified record-plane dialect rules (aws_record_dialect.md, scouted
# against real eu-west-2 on 2026-07-12). The fake REJECTS what real AWS
# rejects so the hermetic tier can never lie about this dialect again.
# ---------------------------------------------------------------------------

#: BatchCreateMemoryRecords per-record input keys. memoryRecordId is NOT
#: legal — the server mints the durable id.
_BATCH_CREATE_ALLOWED_KEYS = frozenset(
    {
        "requestIdentifier",
        "namespaces",
        "content",
        "timestamp",
        "memoryStrategyId",
        "metadata",
    }
)
_BATCH_CREATE_REQUIRED_KEYS = frozenset(
    {"requestIdentifier", "namespaces", "content", "timestamp"}
)

#: BatchUpdateMemoryRecords per-record input keys.
_BATCH_UPDATE_ALLOWED_KEYS = frozenset(
    {
        "memoryRecordId",
        "timestamp",
        "content",
        "namespaces",
        "memoryStrategyId",
        "metadata",
    }
)
_BATCH_UPDATE_REQUIRED_KEYS = frozenset({"memoryRecordId", "timestamp"})

#: Only the CreateMemory indexedKeys are legal metadata-filter keys —
#: mirrors better_memory.cli._agentcore_strategies.INDEXED_KEYS. polarity,
#: outcome, useful_count etc. are all rejected by real AWS.
_VALID_METADATA_FILTER_KEYS = frozenset(
    {"status", "last_credited_at", "overlooked_count"}
)

#: System-managed metadata keys; echoing them back on update is a real 400.
_RESERVED_METADATA_PREFIX = "x-amz-agentcore-memory-"


def _validation_exception(message: str) -> tuple[int, dict[str, Any]]:
    """HTTP 400 ValidationException payload (botocore parses __type/message)."""
    return 400, {"__type": "ValidationException", "message": message}


def _check_metadata_filters(body: Any) -> tuple[int, dict[str, Any]] | None:
    max_results = body.get("maxResults")
    if isinstance(max_results, int) and max_results > 100:
        # Verbatim live constraint (T3 run 2 hit this with maxResults=120).
        return _validation_exception(
            "1 validation error detected: Value at 'maxResults' failed to "
            "satisfy constraint: Member must have value less than or equal "
            "to 100"
        )
    filters = body.get("metadataFilters") or (
        (body.get("searchCriteria") or {}).get("metadataFilters")
    ) or []
    for entry in filters:
        key = (entry.get("left") or {}).get("metadataKey")
        if key not in _VALID_METADATA_FILTER_KEYS:
            # Verbatim live error text (the real message does NOT enumerate
            # the valid set).
            return _validation_exception(
                f"Filter key '{key}' is not a valid filter key"
            )
    if not body.get("namespace") and not body.get("namespacePath"):
        return _validation_exception(
            "At least one of 'namespace' or 'namespacePath' must be provided"
        )
    return None


def _check_batch_update_record_metadata(rec: dict[str, Any]) -> str | None:
    """Return the failedRecords errorMessage for an invalid metadata map."""
    metadata = rec.get("metadata")
    if not isinstance(metadata, dict):
        return None
    reserved = sorted(k for k in metadata if k.startswith(_RESERVED_METADATA_PREFIX))
    if reserved:
        return (
            "Metadata keys cannot use reserved names or prefixes: "
            + ", ".join(reserved)
        )
    last_credited = metadata.get("last_credited_at")
    if isinstance(last_credited, dict) and "stringValue" not in last_credited:
        # last_credited_at is declared STRING in indexedKeys; dateTimeValue
        # (or any other type) fails the whole record update on real AWS.
        return (
            "Metadata key 'last_credited_at' value type does not match "
            "declared indexed key type 'STRING'."
        )
    return None


def dialect_violation(
    operation: str | None, body: Any
) -> tuple[int, dict[str, Any]] | None:
    """Return an overriding ``(http_status, payload)`` when the request
    violates the live-verified record-plane dialect, else ``None`` (the
    canned response is used). Batch-update metadata violations come back the
    way real AWS reports them: HTTP 200 with ``failedRecords`` entries."""
    if not isinstance(body, dict):
        return None

    if operation == "BatchCreateMemoryRecords":
        for i, rec in enumerate(body.get("records") or []):
            unknown = sorted(set(rec) - _BATCH_CREATE_ALLOWED_KEYS)
            if unknown:
                return _validation_exception(
                    f"Unknown parameter in records[{i}]: "
                    f"{', '.join(unknown)}, must be one of: "
                    + ", ".join(sorted(_BATCH_CREATE_ALLOWED_KEYS))
                )
            missing = sorted(_BATCH_CREATE_REQUIRED_KEYS - set(rec))
            if missing:
                return _validation_exception(
                    f"Missing required parameter in records[{i}]: "
                    + ", ".join(missing)
                )
        return None

    if operation in ("ListMemoryRecords", "RetrieveMemoryRecords"):
        return _check_metadata_filters(body)

    if operation == "BatchUpdateMemoryRecords":
        successful: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for i, rec in enumerate(body.get("records") or []):
            unknown = sorted(set(rec) - _BATCH_UPDATE_ALLOWED_KEYS)
            if unknown:
                return _validation_exception(
                    f"Unknown parameter in records[{i}]: "
                    f"{', '.join(unknown)}, must be one of: "
                    + ", ".join(sorted(_BATCH_UPDATE_ALLOWED_KEYS))
                )
            missing = sorted(_BATCH_UPDATE_REQUIRED_KEYS - set(rec))
            if missing:
                return _validation_exception(
                    f"Missing required parameter in records[{i}]: "
                    + ", ".join(missing)
                )
            error_message = _check_batch_update_record_metadata(rec)
            if error_message is not None:
                failed.append(
                    {
                        "memoryRecordId": rec.get("memoryRecordId", ""),
                        "status": "FAILED",
                        "errorCode": 400,
                        "errorMessage": error_message,
                    }
                )
            else:
                successful.append(
                    {
                        "memoryRecordId": rec.get("memoryRecordId", ""),
                        "status": "SUCCEEDED",
                    }
                )
        if failed:
            # Real AWS: HTTP 200, per-record failures in failedRecords —
            # never a raised ClientError.
            return 200, {"successfulRecords": successful, "failedRecords": failed}
        return None

    return None


def _uri_to_regex(request_uri: str) -> re.Pattern[str]:
    """Compile a botocore ``requestUri`` (e.g. ``/memories/{memoryId}/events``)
    into an anchored path regex. ``{param}`` matches one path segment;
    ``{param+}`` (greedy) matches across segments."""
    path = request_uri.split("?", 1)[0]
    parts: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(path):
        parts.append(re.escape(path[pos : match.start()]))
        parts.append(".+" if match.group(1).endswith("+") else "[^/]+")
        pos = match.end()
    parts.append(re.escape(path[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def _load_routes() -> list[tuple[str, str, re.Pattern[str]]]:
    """Build ``(operation_name, http_method, path_regex)`` routes from the
    installed gzipped botocore models for both agentcore planes."""
    data_root = Path(botocore.__file__).resolve().parent
    model_paths = sorted(p for g in _MODEL_GLOBS for p in data_root.glob(g))
    if not model_paths:
        raise FileNotFoundError(
            f"no bedrock-agentcore service models found under {data_root} "
            f"(globs {_MODEL_GLOBS!r}) — is the [agentcore] extra installed?"
        )
    routes: list[tuple[str, str, re.Pattern[str]]] = []
    for model_path in model_paths:
        raw = model_path.read_bytes()
        model = json.loads(gzip.decompress(raw) if model_path.suffix == ".gz" else raw)
        for op_name, op in model.get("operations", {}).items():
            http = op.get("http", {})
            request_uri = http.get("requestUri")
            if not request_uri:
                continue
            routes.append(
                (op_name, http.get("method", "POST"), _uri_to_regex(request_uri))
            )
    return routes


_SIGV4_CREDENTIAL_RE = re.compile(r"Credential=([^,\s]+)")


@dataclass
class RecordedRequest:
    """One captured HTTP request, matched to a botocore operation name."""

    operation: str | None
    method: str
    path: str  # includes any query string
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None  # parsed JSON when possible, raw text otherwise, None if empty

    def header(self, name: str) -> str:
        """Case-insensitive header lookup ('' when absent)."""
        lower = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lower:
                return value
        return ""

    @property
    def host(self) -> str:
        return self.header("Host")

    @property
    def authorization(self) -> str:
        return self.header("Authorization")

    @property
    def sigv4_access_key(self) -> str:
        """Access-key id from the SigV4 ``Credential=`` scope ('' if unsigned)."""
        scope = self._credential_scope()
        return scope[0] if scope else ""

    @property
    def sigv4_region(self) -> str:
        """Signing region from the SigV4 ``Credential=`` scope ('' if unsigned).

        Scope shape: ``<access-key>/<date>/<region>/<service>/aws4_request``
        — the region is positional (index 2)."""
        scope = self._credential_scope()
        return scope[2] if scope and len(scope) >= 3 else ""

    def _credential_scope(self) -> list[str] | None:
        match = _SIGV4_CREDENTIAL_RE.search(self.authorization)
        return match.group(1).split("/") if match else None

    def text(self) -> str:
        """path + serialized body, for model-independent substring asserts
        ('is this memory id anywhere on the wire?')."""
        if self.body is None:
            body_text = ""
        elif isinstance(self.body, str):
            body_text = self.body
        else:
            body_text = json.dumps(self.body)
        return f"{self.path} {body_text}"


class FakeAgentCore:
    """Recording fake for both bedrock-agentcore planes. Context manager."""

    def __init__(self) -> None:
        self._routes = _load_routes()
        self._known_ops = {name for name, _, _ in self._routes}
        self._responses: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.requests: list[RecordedRequest] = []

        fake = self

        class _Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 + explicit Content-Length so urllib3's keep-alive
            # connection reuse works against this server.
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                fake._serve(self)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle
            do_PATCH = _handle

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — base class parameter name; silence stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-agentcore", daemon=True
        )
        self._thread.start()

    # ----- test-facing API -----

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_response(self, operation: str, payload: Any) -> None:
        """Can a response for an operation name (e.g. ``"CreateEvent"``).

        ``payload`` is a JSON-serializable dict, or a callable
        ``RecordedRequest -> dict``. Raises on unknown operation names so a
        typo cannot silently leave the fallback ``{}`` in place."""
        if operation not in self._known_ops:
            raise ValueError(
                f"unknown agentcore operation {operation!r}; "
                f"known: {sorted(self._known_ops)}"
            )
        self._responses[operation] = payload

    def requests_for(self, operation: str) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if r.operation == operation]

    def clear(self) -> None:
        """Drop all recorded requests (canned responses are kept)."""
        with self._lock:
            self.requests.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def __enter__(self) -> FakeAgentCore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----- server side -----

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""

        path_only = handler.path.split("?", 1)[0]
        operation: str | None = None
        for name, method, pattern in self._routes:
            if method == handler.command and pattern.match(path_only):
                operation = name
                break

        body: Any = None
        if raw:
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                body = raw.decode("utf-8", errors="replace")

        record = RecordedRequest(
            operation=operation,
            method=handler.command,
            path=handler.path,
            headers=dict(handler.headers.items()),
            body=body,
        )
        with self._lock:
            self.requests.append(record)

        # Dialect enforcement BEFORE the canned-response lookup: requests
        # real AWS rejects must fail here too, no matter what a test canned.
        override = dialect_violation(operation, body)
        if override is not None:
            status_code, payload = override
        else:
            status_code = 200
            payload = self._responses.get(operation or "", {})
            if callable(payload):
                payload = payload(record)
        data = json.dumps(payload).encode("utf-8")

        handler.send_response(status_code)
        if status_code == 400:
            # botocore's rest-json error parser reads this header (and the
            # __type body member) to raise ClientError ValidationException.
            handler.send_header("x-amzn-ErrorType", "ValidationException")
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
