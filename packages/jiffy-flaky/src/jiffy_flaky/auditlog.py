"""The provider's own record of what it did, one JSON object per line.

This file is the witness. It is written by the provider and by nothing else, so a test
can trust it over anything the system under test says about itself.

Every record is written and flushed under one lock, so the file order is the order in
which things happened. With fsync on, an append waits until its record is on disk, but
the fsync calls are shared: one fsync covers every record flushed before it began, so
fifty threads appending at once cost a couple of fsyncs, not fifty.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import IO, Any

log = logging.getLogger(__name__)

Record = dict[str, Any]


class AuditLog:
    """Appends records to an optional JSONL file and always keeps them in memory too."""

    def __init__(self, path: Path | None = None, *, fsync: bool = True) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._records: list[Record] = []
        self._fsync = fsync
        self._file: IO[str] | None = None
        self._closed = False
        self._written = 0
        self._synced = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    def append(self, kind: str, *, wait_for_fsync: bool = True, **fields: Any) -> Record:
        """Write one record. With wait_for_fsync=False it is flushed now and fsynced later,
        by the next append that waits, or by close()."""
        with self._lock:
            record: Record = {"kind": kind, "at": time.time(), **fields}
            self._records.append(record)
            if self._file is None:
                if self._closed and self.path is not None:
                    log.warning("a %s record arrived after the log was closed; not on disk", kind)
                return record
            self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._file.flush()
            self._written += 1
            position = self._written
        if self._fsync and wait_for_fsync:
            self._sync(position)
        return record

    def records(self) -> list[Record]:
        with self._lock:
            return list(self._records)

    def close(self) -> None:
        with self._sync_lock, self._lock:
            self._closed = True
            if self._file is not None:
                if self._fsync:
                    os.fsync(self._file.fileno())
                self._file.close()
                self._file = None

    def _sync(self, position: int) -> None:
        with self._sync_lock:
            if self._synced >= position:
                return
            with self._lock:
                if self._file is None:
                    return
                target, fd = self._written, self._file.fileno()
            os.fsync(fd)
            self._synced = target
