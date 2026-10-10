"""Receives the provider's status webhooks.

Each event is applied at most once (its id goes into `provider_events` in the same
transaction) and merged through the forward-only state lattice, so duplicates,
reordering and a late `failed` all end in the right place. Anything that goes wrong on
our side answers 503, which makes the provider deliver the event again later.
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from courier import db, states
from courier.outbox import Outbox
from jiffy_lease import Leases

log = logging.getLogger(__name__)


def apply_event(conn: db.Connection, outbox: Outbox, event: dict[str, Any]) -> str:
    """Returns "applied", "duplicate" or "unknown" (no intent has this key)."""
    with conn.transaction():
        inserted = conn.execute(
            "INSERT INTO provider_events (event_id, provider_id, status, sequence)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
            (event["event_id"], event["message_id"], event["status"], event["sequence"]),
        ).rowcount
        if inserted == 0:
            return "duplicate"
        merged = outbox.record(
            conn,
            key=str(event["idempotency_key"]),
            state=states.from_provider(str(event["status"])),
            provider_id=str(event["message_id"]),
        )
    if merged is None:
        log.warning("webhook %s is for a key we never wrote", event["event_id"])
        return "unknown"
    return "applied"


class WebhookServer:
    def __init__(self, database_url: str, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.database_url = database_url
        self.outbox = Outbox(Leases("actions", ttl=60), poll_after=0)
        self._local = threading.local()
        self.http = ThreadingHTTPServer((host, port), self._handler())
        self.http.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.http.server_address[:2]
        return f"http://{host!s}:{port}/webhooks"

    def serve_forever(self) -> None:
        self.http.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.http.shutdown()
        self.http.server_close()

    def handle(self, event: dict[str, Any]) -> HTTPStatus:
        conn: db.Connection | None = getattr(self._local, "conn", None)
        try:
            if conn is None or conn.closed:
                conn = self._local.conn = db.connect(self.database_url)
            apply_event(conn, self.outbox, event)
        except psycopg.Error as exc:
            log.warning("could not apply webhook %s: %s", event.get("event_id"), exc)
            if conn is not None:
                conn.close()
            return HTTPStatus.SERVICE_UNAVAILABLE
        return HTTPStatus.OK

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/webhooks":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    event = json.loads(self.rfile.read(length))
                    status = server.handle(event)
                except (ValueError, KeyError):
                    status = HTTPStatus.BAD_REQUEST
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                log.debug(format, *args)

        return Handler
