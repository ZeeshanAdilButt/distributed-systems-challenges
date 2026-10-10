"""Process-level faults: kill without cleanup, and freeze or thaw a whole process."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable

import psutil

logger = logging.getLogger(__name__)

_STOPPED_OR_GONE = frozenset(
    {psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP, psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
)
_FREEZE_CONFIRM_TIMEOUT = 1.0


def hard_kill(pid: int) -> None:
    """Kill `pid` at once. The victim runs no cleanup: no finally blocks, no atexit, no flush.

    Raises ProcessLookupError if there is no such process.
    """
    if sys.platform == "win32":
        _with_psutil(pid, psutil.Process.kill)
    else:
        os.kill(pid, signal.SIGKILL)


def freeze(pid: int) -> None:
    """Stop every thread of `pid`, the way a long GC pause would.

    Returns once the process is actually stopped. Raises ProcessLookupError if there is
    no such process.
    """
    if sys.platform == "win32":
        _with_psutil(pid, psutil.Process.suspend)
    else:
        os.kill(pid, signal.SIGSTOP)
        _wait_until_stopped(pid)


def thaw(pid: int) -> None:
    """Resume a process stopped by `freeze`. Raises ProcessLookupError if it is gone."""
    if sys.platform == "win32":
        _with_psutil(pid, psutil.Process.resume)
    else:
        os.kill(pid, signal.SIGCONT)


def _with_psutil(pid: int, action: Callable[[psutil.Process], None]) -> None:
    try:
        action(psutil.Process(pid))
    except psutil.NoSuchProcess as exc:
        raise ProcessLookupError(f"no process with pid {pid}") from exc


def _wait_until_stopped(pid: int) -> None:
    # kill() only queues SIGSTOP. The target stops the next time it leaves the kernel,
    # so a write it is in the middle of can still land after kill() returns.
    deadline = time.monotonic() + _FREEZE_CONFIRM_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() in _STOPPED_OR_GONE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.001)
    logger.warning("pid %d has not stopped yet; SIGSTOP is still pending", pid)
