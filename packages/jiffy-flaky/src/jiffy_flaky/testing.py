"""Helpers for tests that talk to jiffy-flaky: a small client and a webhook receiver."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any
from urllib.parse import quote, urlencode, urlsplit


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any


def request_json(
    method: str,
    url: str,
    *,
    body: object = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> Response:
    """One HTTP call with a JSON answer. Raises TimeoutError on timeout.

    The body is sent as JSON, except bytes, which are sent as they are.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    connection = http.client.HTTPConnection(parts.netloc, timeout=timeout)
    try:
        connection.request(method, path, data, all_headers)
        response = connection.getresponse()
        raw = response.read()
        return Response(response.status, json.loads(raw) if raw else None)
    finally:
        connection.close()


class FlakyClient:
    """Calls a running provider and hands back the raw status and JSON body."""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(
        self,
        key: str | None,
        *,
        to: str = "+15550100",
        body: str = "hello",
        metadata: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response:
        payload: dict[str, Any] = {"to": to, "body": body}
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        return request_json(
            "POST",
            f"{self.base_url}/v1/messages",
            body=payload,
            headers={} if key is None else {"Idempotency-Key": key},
            timeout=self.timeout if timeout is None else timeout,
        )

    def lookup(self, key: str, *, timeout: float | None = None) -> Response:
        return request_json(
            "GET",
            f"{self.base_url}/v1/messages?{urlencode({'idempotency_key': key})}",
            timeout=self.timeout if timeout is None else timeout,
        )

    def get(self, message_id: str, *, timeout: float | None = None) -> Response:
        return request_json(
            "GET",
            f"{self.base_url}/v1/messages/{quote(message_id)}",
            timeout=self.timeout if timeout is None else timeout,
        )


class _RecorderServer(ThreadingHTTPServer):
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], recorder: WebhookRecorder) -> None:
        self.recorder = recorder
        super().__init__(address, _RecorderHandler)


class _RecorderHandler(BaseHTTPRequestHandler):
    server: _RecorderServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        event = json.loads(self.rfile.read(length) or b"null")
        status = self.server.recorder._receive(event)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class WebhookRecorder:
    """Accepts webhooks and keeps every accepted event in arrival order.

    `fail_next(n)` answers the next n requests with a 500, to exercise retries. Stop it
    and start a new one on the same port to play a receiver that crashed and came back.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, fail_first: int = 0) -> None:
        self._address = (host, port)
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._failures_left = fail_first
        self._requests = 0
        self._server: _RecorderServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> WebhookRecorder:
        self._server = _RecorderServer(self._address, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the recorder has not been started")
        return f"http://{self._address[0]}:{self._server.server_port}/webhooks"

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._cond:
            return list(self._events)

    @property
    def requests(self) -> int:
        """Every request received, accepted or failed on purpose."""
        with self._cond:
            return self._requests

    def fail_next(self, n: int) -> None:
        with self._cond:
            self._failures_left = n

    def wait_for(self, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Wait until at least count events were accepted, then return all of them."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self._events) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"got {len(self._events)} of {count} webhook events")
                self._cond.wait(remaining)
            return list(self._events)

    def _receive(self, event: dict[str, Any]) -> int:
        with self._cond:
            self._requests += 1
            if self._failures_left > 0:
                self._failures_left -= 1
                return 500
            self._events.append(event)
            self._cond.notify_all()
            return 200

    def __enter__(self) -> WebhookRecorder:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
