"""Command line: jiffy-chaos pg up|down|restart|status."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

from jiffy_chaos.postgres import LocalCluster, PgController, from_env


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environ = dict(os.environ)
    if args.mode is not None:
        environ["JIFFY_PG_MODE"] = args.mode
    try:
        controller = from_env(environ)
        return _ACTIONS[args.action](controller, args.immediate)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"jiffy-chaos: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jiffy-chaos")
    commands = parser.add_subparsers(dest="command", required=True)
    pg = commands.add_parser("pg", help="control the Postgres that JIFFY_PG_MODE selects")
    pg.add_argument("action", choices=sorted(_ACTIONS))
    pg.add_argument(
        "--mode",
        choices=["local", "docker", "kubernetes"],
        help="overrides JIFFY_PG_MODE",
    )
    pg.add_argument(
        "--immediate",
        action="store_true",
        help="down and restart without a clean shutdown, like a crash",
    )
    return parser


def _up(controller: PgController, immediate: bool) -> int:
    if isinstance(controller, LocalCluster):
        controller.init()
    controller.start()
    controller.wait_ready()
    print(controller.url)
    return 0


def _down(controller: PgController, immediate: bool) -> int:
    controller.stop("immediate" if immediate else "fast")
    return 0


def _restart(controller: PgController, immediate: bool) -> int:
    controller.restart("immediate" if immediate else "fast")
    controller.wait_ready()
    print(controller.url)
    return 0


def _status(controller: PgController, immediate: bool) -> int:
    ready = controller.is_ready()
    print("ready" if ready else "not ready")
    return 0 if ready else 1


_ACTIONS: dict[str, Callable[[PgController, bool], int]] = {
    "up": _up,
    "down": _down,
    "restart": _restart,
    "status": _status,
}
