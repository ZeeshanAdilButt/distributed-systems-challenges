"""The interface every Postgres controller implements, and the helpers they share."""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

import psycopg

logger = logging.getLogger(__name__)

type StopMode = Literal["smart", "fast", "immediate"]
type RestartMode = Literal["fast", "immediate"]
type Runner = Callable[[Sequence[str]], None]

_READY_POLL_INTERVAL = 0.1


class PgController(Protocol):
    """Start, stop and crash one Postgres server, wherever it runs.

    stop and restart with mode "immediate" behave like a crash: no clean shutdown, so
    the next start runs crash recovery.
    """

    @property
    def url(self) -> str:
        """A libpq URL that psycopg can connect with."""
        ...

    def start(self) -> None: ...

    def stop(self, mode: StopMode = "fast") -> None: ...

    def restart(self, mode: RestartMode = "immediate") -> None: ...

    def wait_ready(self, timeout: float = 30.0) -> None:
        """Block until a connection succeeds. Raises TimeoutError after `timeout` seconds."""
        ...

    def is_ready(self) -> bool: ...


def run_command(argv: Sequence[str]) -> None:
    """The default Runner: run argv and raise CalledProcessError if it fails."""
    logger.info("running %s", shlex.join(argv))
    subprocess.run(list(argv), check=True)


def accepts_connections(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def wait_until_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not accepts_connections(url):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Postgres did not accept connections within {timeout:g}s")
        time.sleep(_READY_POLL_INTERVAL)
