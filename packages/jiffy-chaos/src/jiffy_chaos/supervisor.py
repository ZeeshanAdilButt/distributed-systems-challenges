"""Run a fleet of real processes, restart the ones that die, and break them on purpose."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from jiffy_chaos import signals

logger = logging.getLogger(__name__)

type EventKind = Literal["started", "exited", "killed", "frozen", "thawed", "restarted"]

_POLL_INTERVAL = 0.01
_REAP_TIMEOUT = 5.0
_RELAUNCH_RETRY = 1.0


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """How to run one supervised process.

    `env` is merged over os.environ. Child stdout and stderr are appended to
    `stdout_path`, or discarded when it is None.
    """

    name: str
    argv: Sequence[str]
    env: Mapping[str, str] | None = None
    cwd: str | None = None
    restart: bool = True
    restart_delay: float = 0.2
    stdout_path: Path | None = None


@dataclass(frozen=True, slots=True)
class SupervisorEvent:
    """Something that happened to a supervised process. `at` is time.monotonic()."""

    at: float
    name: str
    pid: int
    kind: EventKind
    detail: str = ""


@dataclass(slots=True)
class _Child:
    spec: ProcessSpec
    process: subprocess.Popen[bytes] | None = None
    exit_recorded: bool = False
    restart_at: float | None = None
    frozen: bool = False

    def live_process(self) -> subprocess.Popen[bytes] | None:
        if self.process is None or self.process.poll() is not None:
            return None
        return self.process


class Supervisor:
    """Keeps a set of named processes running and lets a test kill, freeze or thaw them.

    A monitor thread restarts any process that exits, after its spec's restart_delay,
    unless the spec says restart=False or the supervisor is stopping. Every method is
    safe to call from any thread.
    """

    def __init__(self, specs: Sequence[ProcessSpec], *, log_path: Path | None = None) -> None:
        names = [spec.name for spec in specs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate process names: {', '.join(duplicates)}")
        self._children = {spec.name: _Child(spec) for spec in specs}
        self._log_path = log_path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._events: list[SupervisorEvent] = []
        self._restarts = dict.fromkeys(self._children, 0)
        self._monitor: threading.Thread | None = None
        self._stopping = False

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._children)

    @property
    def restarts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._restarts)

    @property
    def events(self) -> list[SupervisorEvent]:
        with self._lock:
            return list(self._events)

    def start(self) -> None:
        with self._lock:
            if self._monitor is not None or self._stopping:
                raise RuntimeError("a Supervisor can only be started once")
            try:
                for child in self._children.values():
                    self._launch(child, "started")
            except OSError:
                self._shutdown(timeout=0)
                raise
            self._monitor = threading.Thread(
                target=self._watch, name="jiffy-chaos-supervisor", daemon=True
            )
            self._monitor.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop restarting, send SIGTERM to every process, and hard kill whatever is
        still alive after `timeout` seconds."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        self._wake.set()
        if self._monitor is not None:
            self._monitor.join()
        with self._lock:
            self._shutdown(timeout)

    def running(self) -> dict[str, int]:
        """Name to pid for every process that is alive right now, frozen ones included."""
        with self._lock:
            return {
                name: process.pid
                for name, child in self._children.items()
                if (process := child.live_process()) is not None
            }

    def pid(self, name: str) -> int | None:
        with self._lock:
            process = self._children[name].live_process()
            return None if process is None else process.pid

    def kill(self, name: str) -> int:
        """Hard kill the current process for `name` and return its pid.

        Returns after the process has been reaped. Raises ProcessLookupError if `name`
        is not running.
        """
        with self._lock:
            process = self._require_running(name)
            signals.hard_kill(process.pid)
            self._record(name, process.pid, "killed")
            try:
                process.wait(_REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning("%s (pid %d) is still alive after SIGKILL", name, process.pid)
            return process.pid

    def freeze(self, name: str) -> int:
        """Stop every thread of `name`'s process and return its pid."""
        with self._lock:
            process = self._require_running(name)
            signals.freeze(process.pid)
            self._children[name].frozen = True
            self._record(name, process.pid, "frozen")
            return process.pid

    def thaw(self, name: str) -> int:
        """Resume `name`'s process after `freeze` and return its pid."""
        with self._lock:
            process = self._require_running(name)
            signals.thaw(process.pid)
            self._children[name].frozen = False
            self._record(name, process.pid, "thawed")
            return process.pid

    def _require_running(self, name: str) -> subprocess.Popen[bytes]:
        process = self._children[name].live_process()
        if process is None:
            raise ProcessLookupError(f"{name} is not running")
        return process

    def _watch(self) -> None:
        while not self._wake.wait(_POLL_INTERVAL):
            with self._lock:
                if self._stopping:
                    return
                now = time.monotonic()
                for child in self._children.values():
                    if self._note_exit(child) and child.spec.restart:
                        child.restart_at = now + child.spec.restart_delay
                    if child.restart_at is not None and now >= child.restart_at:
                        self._relaunch(child)

    def _relaunch(self, child: _Child) -> None:
        try:
            self._launch(child, "restarted")
        except OSError:
            logger.exception("could not restart %s; retrying", child.spec.name)
            child.restart_at = time.monotonic() + max(child.spec.restart_delay, _RELAUNCH_RETRY)

    def _launch(self, child: _Child, kind: EventKind) -> None:
        process = _spawn(child.spec)
        child.process = process
        child.exit_recorded = False
        child.restart_at = None
        child.frozen = False
        detail = ""
        if kind == "restarted":
            self._restarts[child.spec.name] += 1
            detail = f"restart {self._restarts[child.spec.name]}"
        self._record(child.spec.name, process.pid, kind, detail)

    def _note_exit(self, child: _Child) -> bool:
        """Record the exit of `child`'s process once. True if it was recorded now."""
        process = child.process
        if process is None or child.exit_recorded:
            return False
        code = process.poll()
        if code is None:
            return False
        child.exit_recorded = True
        child.frozen = False
        self._record(child.spec.name, process.pid, "exited", f"returncode={code}")
        return True

    def _shutdown(self, timeout: float) -> None:
        live = [
            (child, process)
            for child in self._children.values()
            if (process := child.live_process()) is not None
        ]
        for child, process in live:
            process.terminate()
            if child.frozen:
                # A stopped process cannot act on SIGTERM until it is resumed.
                with suppress(ProcessLookupError):
                    signals.thaw(process.pid)
        deadline = time.monotonic() + timeout
        for child, process in live:
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning("%s ignored SIGTERM; killing it", child.spec.name)
                signals.hard_kill(process.pid)
                process.wait()
        for child in self._children.values():
            child.restart_at = None
            self._note_exit(child)

    def _record(self, name: str, pid: int, kind: EventKind, detail: str = "") -> None:
        self._events.append(SupervisorEvent(time.monotonic(), name, pid, kind, detail))
        logger.info("%s %s pid=%d %s", kind, name, pid, detail)
        if self._log_path is not None:
            stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
            line = f"{stamp} {kind} {name} pid={pid} {detail}".rstrip()
            with self._log_path.open("a", encoding="utf-8") as log:
                log.write(line + "\n")


def _spawn(spec: ProcessSpec) -> subprocess.Popen[bytes]:
    env = None if spec.env is None else {**os.environ, **spec.env}
    argv = list(spec.argv)
    if spec.stdout_path is None:
        return subprocess.Popen(
            argv,
            env=env,
            cwd=spec.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with spec.stdout_path.open("ab") as out:
        return subprocess.Popen(
            argv,
            env=env,
            cwd=spec.cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
