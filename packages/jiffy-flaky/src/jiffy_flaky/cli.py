"""`jiffy-flaky serve` runs the provider. `jiffy-flaky audit LOG` checks a finished run."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jiffy_flaky.audit import audit, read_log
from jiffy_flaky.config import FlakyConfig, parse_field
from jiffy_flaky.provider import FlakyProvider

ENV_PREFIX = "FLAKY_"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        overrides = {
            field.name: getattr(args, field.name)
            for field in dataclasses.fields(FlakyConfig)
            if hasattr(args, field.name)
        }
        return _serve(overrides)
    return _audit(args.log, group_by=args.group_by, expect_ids=args.expect_ids, as_json=args.json)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiffy-flaky", description="A messaging provider that misbehaves on purpose."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve",
        help="run the provider until SIGINT or SIGTERM",
        description=f"Every flag falls back to a {ENV_PREFIX}* environment variable, "
        "then to the built-in default.",
    )
    for field in dataclasses.fields(FlakyConfig):
        flag = "--" + field.name.replace("_", "-")
        flags = ["--log", flag] if field.name == "log_path" else [flag]
        help_text = f"env {ENV_PREFIX}{field.name.upper()}, default {field.default}"
        if field.name == "fsync":
            serve.add_argument(
                *flags,
                dest=field.name,
                action=argparse.BooleanOptionalAction,
                default=argparse.SUPPRESS,
                help=help_text,
            )
        else:
            serve.add_argument(
                *flags,
                dest=field.name,
                type=_converter(field.name),
                default=argparse.SUPPRESS,
                metavar=field.name.upper(),
                help=help_text,
            )

    check = commands.add_parser(
        "audit", help="check a log; exit 0 when no action has a duplicate or missing effect"
    )
    check.add_argument("log", type=Path, help="the JSONL log written by `serve --log`")
    check.add_argument(
        "--group-by", default="action_id", help="metadata field that identifies an action"
    )
    check.add_argument("--expect-ids", type=Path, help="file with one expected action id per line")
    check.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _converter(name: str) -> Callable[[str], Any]:
    def convert(text: str) -> Any:
        try:
            return parse_field(name, text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    return convert


def _serve(overrides: dict[str, Any]) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = FlakyConfig.from_env(ENV_PREFIX, **overrides)
    except ValueError as exc:
        print(f"jiffy-flaky: {exc}", file=sys.stderr)
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with FlakyProvider(config) as provider:
        print(f"jiffy-flaky listening on {provider.url}", flush=True)
        stop.wait()
    return 0


def _audit(log: Path, *, group_by: str, expect_ids: Path | None, as_json: bool) -> int:
    try:
        records = read_log(log)
        expected = None
        if expect_ids is not None:
            lines = expect_ids.read_text(encoding="utf-8").splitlines()
            expected = [line.strip() for line in lines if line.strip()]
    except (OSError, ValueError) as exc:
        print(f"jiffy-flaky: {exc}", file=sys.stderr)
        return 2
    report = audit(records, group_by=group_by, expected=expected)
    print(json.dumps(report.to_json(), indent=2) if as_json else report.summary())
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
