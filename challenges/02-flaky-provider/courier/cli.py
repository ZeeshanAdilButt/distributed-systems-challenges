"""`courier init | worker | webhooks | enqueue | status`. Settings come from COURIER_* env."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from types import FrameType

from courier import db, intake
from courier.config import Settings
from courier.webhooks import WebhookServer
from courier.worker import Worker


def _on_term(stop: threading.Event) -> None:
    def handler(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _database_url() -> str:
    url = os.environ.get("COURIER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("set COURIER_DATABASE_URL or DATABASE_URL")
    return url


def cmd_init(args: argparse.Namespace) -> int:
    with db.connect(_database_url()) as conn:
        db.create_schema(conn)
        intake.seed_stores(conn, args.stores, max_inflight=args.max_inflight)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    worker = Worker(Settings.from_env())
    _on_term(worker.stopping)
    worker.run()
    return 0


def cmd_webhooks(args: argparse.Namespace) -> int:
    server = WebhookServer(_database_url(), host=args.host, port=args.port)
    stop = threading.Event()
    _on_term(stop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"courier webhooks listening on {server.url}", flush=True)
    stop.wait()
    server.shutdown()
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    rows = (
        (1 + i % args.stores, f"+1555{i:07d}", "Your order has shipped.") for i in range(args.count)
    )
    with db.connect(_database_url()) as conn:
        print(intake.enqueue_many(conn, rows))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with db.connect(_database_url()) as conn:
        for status, outcome, n in conn.execute(
            "SELECT status, coalesce(outcome, ''), count(*)"
            " FROM actions GROUP BY 1, 2 ORDER BY 1, 2"
        ):
            print(f"{status:<11} {outcome:<10} {n}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="courier")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create tables and stores")
    init.add_argument("--stores", type=int, default=100)
    init.add_argument("--max-inflight", type=int, default=2)
    init.set_defaults(run=cmd_init)

    sub.add_parser("worker", help="claim and deliver actions until stopped").set_defaults(
        run=cmd_worker
    )

    hooks = sub.add_parser("webhooks", help="receive provider webhooks")
    hooks.add_argument("--host", default="127.0.0.1")
    hooks.add_argument("--port", type=int, default=8701)
    hooks.set_defaults(run=cmd_webhooks)

    enqueue = sub.add_parser("enqueue", help="add actions spread across stores")
    enqueue.add_argument("--count", type=int, required=True)
    enqueue.add_argument("--stores", type=int, default=100)
    enqueue.set_defaults(run=cmd_enqueue)

    sub.add_parser("status", help="count actions by status").set_defaults(run=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("COURIER_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(process)d %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
