"""A small cache inside each API process, bounded by entry count, least recently used out.

It exists so that losing the shared cache (Valkey, Memcached) does not lose every copy of
a response at once. Two rules matter more than the eviction policy:

- ``put`` stores the entry as given, timestamps and all. It never re-stamps.
- ``put`` never replaces a newer read with an older one. Two refreshes can finish out of
  order; the one that read the database later wins.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from jiffy_cache.entry import Entry


class LocalCache[V]:
    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self._entries: OrderedDict[str, Entry[V]] = OrderedDict()
        self._lock = threading.Lock()
        self.evictions = 0

    def get(self, key: str) -> Entry[V] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, entry: Entry[V]) -> bool:
        """Keep ``entry`` unless the cache already holds a newer read. True if stored."""
        with self._lock:
            held = self._entries.get(entry.key)
            if held is not None and held.source_read_at > entry.source_read_at:
                return False
            self._entries[entry.key] = entry
            self._entries.move_to_end(entry.key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1
            return True

    def drop(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
