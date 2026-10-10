"""A throwaway Postgres cluster run from local server binaries (initdb, pg_ctl)."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Self

import psutil

from jiffy_chaos.postgres.controller import (
    RestartMode,
    StopMode,
    accepts_connections,
    wait_until_ready,
)
from jiffy_chaos.signals import freeze, hard_kill

logger = logging.getLogger(__name__)

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_SEARCH_ROOTS = (
    (Path("/usr/lib/postgresql"), "*/bin"),
    (Path("/usr/local/pgsql"), "bin"),
    (Path("/opt/homebrew/opt"), "postgresql@*/bin"),
    (Path("/usr/local/opt"), "postgresql@*/bin"),
    (Path("C:/Program Files/PostgreSQL"), "*/bin"),
)
_PG_CTL_WAIT_SECONDS = "60"
_CRASH_TIMEOUT = 10.0
_LOG_TAIL_LINES = 20


def find_pg_bin(environ: Mapping[str, str] = os.environ) -> Path | None:
    """Find a directory that holds initdb and pg_ctl.

    Looks at $PG_BIN, then `pg_config --bindir`, then common install locations, newest
    version first. Returns None if nothing usable is found.
    """
    return next((path for path in _candidate_bindirs(environ) if _has_server_binaries(path)), None)


class LocalCluster:
    """A Postgres cluster in `datadir`, listening on 127.0.0.1:`port` with trust auth.

    The unix socket goes in `socket_dir`, or the datadir itself (POSIX only). When `port`
    is None, the port of a server already running from `datadir` is reused, otherwise a
    free port is picked. As a context manager it runs init and start, then destroy.
    """

    __slots__ = ("bindir", "datadir", "port", "socket_dir", "user")

    def __init__(
        self,
        datadir: Path,
        *,
        port: int | None = None,
        bindir: Path | None = None,
        user: str = "postgres",
        socket_dir: Path | None = None,
    ) -> None:
        found = bindir or find_pg_bin()
        if found is None:
            raise FileNotFoundError(
                "PostgreSQL server binaries not found; set PG_BIN to the directory "
                "that holds initdb and pg_ctl"
            )
        self.bindir = found
        self.datadir = Path(datadir).absolute()
        self.user = user
        self.socket_dir = Path(socket_dir).absolute() if socket_dir else self.datadir
        self.port = port or self._running_port() or _free_port()

    def __enter__(self) -> Self:
        self.init()
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.destroy()

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}@127.0.0.1:{self.port}/postgres"

    @property
    def log_path(self) -> Path:
        """The server log. pg_ctl appends to it on every start."""
        return self.datadir / "server.log"

    def init(self) -> None:
        """Run initdb, unless the datadir already has something in it."""
        if self.datadir.is_dir() and any(self.datadir.iterdir()):
            return
        initdb = self.bindir / f"initdb{_EXE_SUFFIX}"
        _run(
            [
                str(initdb),
                "--pgdata",
                str(self.datadir),
                "--username",
                self.user,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
                # Skips fsync of the files initdb writes. Only an OS crash during setup
                # could lose them, and the server itself still runs with fsync=on.
                "--no-sync",
            ]
        )

    def is_running(self) -> bool:
        return self._postmaster() is not None

    def start(self) -> None:
        if self.is_running():
            return
        options = f"-p {self.port} -h 127.0.0.1"
        if sys.platform != "win32":
            # pg_ctl hands -o to /bin/sh, so the socket path must be shell quoted.
            options += f" -k {shlex.quote(str(self.socket_dir))}"
        log = str(self.log_path)
        self._pg_ctl("start", "-w", "-t", _PG_CTL_WAIT_SECONDS, "-l", log, "-o", options)

    def stop(self, mode: StopMode = "fast") -> None:
        """Shut the server down with `pg_ctl stop -m mode`. Does nothing if it is not running.

        "immediate" skips the shutdown checkpoint, so the next start runs crash recovery.
        """
        if self.is_running():
            self._pg_ctl("stop", "-w", "-t", _PG_CTL_WAIT_SECONDS, "-m", mode)

    def restart(self, mode: RestartMode = "immediate") -> None:
        """Stop and start again. "immediate" SIGKILLs the postmaster and every backend,
        the same as pulling the plug on the processes, and the start runs crash recovery.
        """
        if mode == "immediate":
            self._crash()
        else:
            self.stop("fast")
        self.start()

    def wait_ready(self, timeout: float = 30.0) -> None:
        wait_until_ready(self.url, timeout)

    def is_ready(self) -> bool:
        return accepts_connections(self.url)

    def destroy(self) -> None:
        """Stop the server if it is running and delete the datadir."""
        # An immediate shutdown still releases the server's System V shared memory,
        # which a SIGKILL would leak once the datadir is gone.
        self.stop("immediate")
        if self.datadir.exists():
            shutil.rmtree(self.datadir)

    def _crash(self) -> None:
        postmaster = self._postmaster()
        if postmaster is None:
            return
        # A stopped postmaster cannot fork new backends while we collect its children.
        freeze(postmaster.pid)
        victims = [postmaster, *postmaster.children(recursive=True)]
        for victim in victims:
            with suppress(ProcessLookupError):
                hard_kill(victim.pid)
        _wait_gone(victims, _CRASH_TIMEOUT)

    def _postmaster(self) -> psutil.Process | None:
        lines = self._pid_file_lines()
        if not lines:
            return None
        try:
            process = psutil.Process(int(lines[0]))
            if process.status() != psutil.STATUS_ZOMBIE and process.name().startswith("postgres"):
                return process
        except (ValueError, psutil.Error):
            pass
        return None

    def _running_port(self) -> int | None:
        # postmaster.pid holds the pid on line 1 and the port on line 4.
        lines = self._pid_file_lines()
        if len(lines) < 4 or self._postmaster() is None:
            return None
        with suppress(ValueError):
            return int(lines[3])
        return None

    def _pid_file_lines(self) -> list[str]:
        try:
            return (self.datadir / "postmaster.pid").read_text().splitlines()
        except OSError:
            return []

    def _pg_ctl(self, command: str, *args: str) -> None:
        pg_ctl = self.bindir / f"pg_ctl{_EXE_SUFFIX}"
        _run([str(pg_ctl), command, "-D", str(self.datadir), *args], server_log=self.log_path)


def _candidate_bindirs(environ: Mapping[str, str]) -> Iterator[Path]:
    if pg_bin := environ.get("PG_BIN"):
        yield Path(pg_bin)
    if (from_pg_config := _pg_config_bindir()) is not None:
        yield from_pg_config
    for root, pattern in _SEARCH_ROOTS:
        yield from sorted(root.glob(pattern), key=_version_key, reverse=True)


def _pg_config_bindir() -> Path | None:
    pg_config = shutil.which("pg_config")
    if pg_config is None:
        return None
    result = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, check=False)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _version_key(bindir: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", bindir.parent.name))


def _has_server_binaries(bindir: Path) -> bool:
    return all((bindir / f"{name}{_EXE_SUFFIX}").is_file() for name in ("initdb", "pg_ctl"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _run(argv: Sequence[str], *, server_log: Path | None = None) -> None:
    logger.info("running %s", shlex.join(argv))
    result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    message = f"{shlex.join(argv)} exited with {result.returncode}\n{result.stdout}{result.stderr}"
    if server_log is not None and server_log.exists():
        tail = server_log.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINES:]
        message += f"\nlast lines of {server_log}:\n" + "\n".join(tail)
    raise RuntimeError(message)


def _wait_gone(processes: Sequence[psutil.Process], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not all(_is_gone(process) for process in processes):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Postgres processes still alive {timeout:g}s after SIGKILL")
        time.sleep(0.01)


def _is_gone(process: psutil.Process) -> bool:
    # A zombie has released its memory and shared memory attachments, which is all a
    # restart cares about. Nobody may reap it if our test runner is PID 1 in a container.
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
