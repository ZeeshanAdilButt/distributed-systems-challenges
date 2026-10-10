"""The HTTP front of the provider, and the object that starts and stops everything."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from jiffy_flaky.auditlog import AuditLog, Record
from jiffy_flaky.config import FlakyConfig
from jiffy_flaky.scheduler import Scheduler
from jiffy_flaky.service import NOT_FOUND, Reply, Service
from jiffy_flaky.stats import Counters
from jiffy_flaky.webhooks import WebhookDispatcher

log = logging.getLogger(__name__)

MESSAGES_PATH = "/v1/messages"


class _Server(ThreadingHTTPServer):
    # Handler threads are joined on close, so stop() returns only after every
    # in-flight request has written its records.
    daemon_threads = False
    # The socketserver default of 5 drops connections when many workers send at once.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], service: Service) -> None:
        self.service = service
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "jiffy-flaky"
    timeout = 30.0  # a connection that never sends a request cannot block stop() forever

    def do_GET(self) -> None:
        received_at = time.time()
        url = urlsplit(self.path)
        service = self.server.service
        if url.path == "/_health":
            reply = Reply(200, {"ok": True})
        elif url.path == "/_stats":
            reply = Reply(200, service.stats())
        elif url.path == MESSAGES_PATH:
            keys = parse_qs(url.query, keep_blank_values=True).get("idempotency_key")
            reply = service.lookup(keys[0] if keys else None, received_at)
        elif url.path.startswith(MESSAGES_PATH + "/") and len(url.path) > len(MESSAGES_PATH) + 1:
            reply = service.get(unquote(url.path[len(MESSAGES_PATH) + 1 :]), received_at)
        else:
            reply = NOT_FOUND
        self._reply(reply)

    def do_POST(self) -> None:
        received_at = time.time()
        if urlsplit(self.path).path != MESSAGES_PATH:
            self._reply(NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload: object = json.loads(raw)
        except ValueError:
            payload = None
        key = self.headers.get("Idempotency-Key")
        self._reply(self.server.service.send(key, payload, received_at))

    def _reply(self, reply: Reply) -> None:
        data = json.dumps(reply.body).encode()
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            log.debug("client left before the %d reply to %s", reply.status, self.path)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)


@dataclass(frozen=True, slots=True)
class _Running:
    audit_log: AuditLog
    counters: Counters
    scheduler: Scheduler
    webhooks: WebhookDispatcher | None
    service: Service
    server: _Server
    thread: threading.Thread


class FlakyProvider:
    """A fake messaging provider on a background thread. Use start() and stop(), or `with`."""

    def __init__(self, config: FlakyConfig | None = None) -> None:
        self.config = config if config is not None else FlakyConfig()
        self._running: _Running | None = None
        self._stopped = False
        self._lock = threading.Lock()

    def start(self) -> FlakyProvider:
        with self._lock:
            if self._running is not None:
                raise RuntimeError("a FlakyProvider can only be started once")
            config = self.config
            audit_log = AuditLog(config.log_path, fsync=config.fsync)
            counters = Counters()
            scheduler = Scheduler()
            webhooks = (
                WebhookDispatcher(config, audit_log, counters, scheduler)
                if config.webhook_url
                else None
            )
            service = Service(config, audit_log, counters, scheduler, webhooks)
            try:
                server = _Server((config.host, config.port), service)
            except OSError:
                audit_log.close()
                raise
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="jiffy-flaky-http",
                daemon=True,
            )
            self._running = _Running(
                audit_log, counters, scheduler, webhooks, service, server, thread
            )
            scheduler.start()
            thread.start()
        log.info("jiffy-flaky listening on %s", self.url)
        return self

    def stop(self) -> None:
        with self._lock:
            running = self._running
            if running is None or self._stopped:
                return
            self._stopped = True
        running.service.stop()
        running.server.shutdown()
        running.server.server_close()
        running.thread.join()
        running.scheduler.stop()
        if running.webhooks is not None:
            running.webhooks.stop()
        running.audit_log.close()

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self._require().server.server_port}"

    def stats(self) -> dict[str, int]:
        return self._require().counters.snapshot()

    def records(self) -> list[Record]:
        """Every audit record written so far, oldest first. Works after stop() too."""
        return self._require().audit_log.records()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until no status change or webhook is still due. False if it timed out."""
        running = self._require()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Scheduler first: a status change can create webhook work, never the reverse.
            idle = running.scheduler.pending() == 0
            if idle and running.webhooks is not None:
                idle = not running.webhooks.busy()
            if idle:
                return True
            time.sleep(0.01)
        return False

    def __enter__(self) -> FlakyProvider:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _require(self) -> _Running:
        if self._running is None:
            raise RuntimeError("the provider has not been started")
        return self._running
