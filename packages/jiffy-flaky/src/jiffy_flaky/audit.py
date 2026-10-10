"""Check a run against the provider's own log.

An effect record is written exactly once for every message the provider created, so
counting effects per action tells you what really happened, whatever the system under
test believes. Group effects by a metadata field (by default `action_id`) and the
questions become simple: did any action produce two messages, and did any produce none?
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NO_GROUP = "<none>"


@dataclass(frozen=True, slots=True)
class AuditReport:
    effects: int
    groups: int
    duplicates: dict[str, int]
    missing: list[str]
    unexpected: list[str]
    final_status: dict[str, str]
    sends_without_lookup: list[str]
    faults: dict[str, int]

    def ok(self) -> bool:
        """No action produced more than one effect, and every expected action produced one."""
        return not self.duplicates and not self.missing and not self.unexpected

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok()}

    def summary(self) -> str:
        statuses = Counter(self.final_status.values())
        rows = [
            ("effects", f"{self.effects} across {self.groups} groups"),
            ("duplicates", _listing([f"{g} (x{n})" for g, n in self.duplicates.items()])),
            ("missing", _listing(self.missing)),
            ("unexpected", _listing(self.unexpected)),
            ("sends without lookup", _listing(self.sends_without_lookup)),
            ("final status", ", ".join(f"{s} {n}" for s, n in sorted(statuses.items()))),
            ("faults", ", ".join(f"{name} {n}" for name, n in self.faults.items())),
            ("result", "OK" if self.ok() else "FAILED"),
        ]
        return "\n".join(f"{label:<22}{value}" for label, value in rows)


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL audit log. A half-written last line, left by a crash, is skipped."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            if number == len(lines):
                log.warning("skipping a partly written last line in %s", path)
                break
            raise ValueError(f"{path}:{number} is not valid JSON") from None
    return records


def audit(
    records: Iterable[Mapping[str, Any]],
    *,
    group_by: str = "action_id",
    expected: Collection[str] | None = None,
) -> AuditReport:
    rows = list(records)
    effects = [r for r in rows if r.get("kind") == "effect"]
    per_group: Counter[str] = Counter()
    last_message: dict[str, str] = {}
    for effect in effects:
        group = _group_of(effect, group_by)
        per_group[group] += 1
        last_message[group] = effect["message_id"]

    latest = _latest_status(rows)
    expected_set = set(expected) if expected is not None else None
    return AuditReport(
        effects=len(effects),
        groups=len(per_group),
        duplicates={group: n for group, n in per_group.items() if n > 1},
        missing=sorted(expected_set - per_group.keys()) if expected_set is not None else [],
        unexpected=(sorted(per_group.keys() - expected_set) if expected_set is not None else []),
        final_status={g: latest[m] for g, m in last_message.items() if m in latest},
        sends_without_lookup=_sends_without_lookup(rows),
        faults=_faults(rows),
    )


def _group_of(effect: Mapping[str, Any], group_by: str) -> str:
    metadata = effect.get("metadata")
    if isinstance(metadata, Mapping) and group_by in metadata:
        return str(metadata[group_by])
    return NO_GROUP


def _latest_status(records: list[Mapping[str, Any]]) -> dict[str, str]:
    """message id -> the status with the highest sequence seen for it."""
    best: dict[str, tuple[int, str]] = {}
    for r in records:
        if r.get("kind") != "status":
            continue
        current = best.get(r["message_id"])
        if current is None or r["sequence"] > current[0]:
            best[r["message_id"]] = (r["sequence"], r["status"])
    return {message_id: status for message_id, (_, status) in best.items()}


def _sends_without_lookup(records: list[Mapping[str, Any]]) -> list[str]:
    """Keys that were sent again without a lookup_miss for that key since the last send.

    A caller that is unsure whether its first send landed should look before it sends
    again. Relying on the idempotency key alone is only safe within the provider's TTL.
    """
    events = sorted(
        (
            r
            for r in records
            if r.get("kind") == "request"
            and r.get("idempotency_key") is not None
            and (
                (r.get("op") == "send" and r.get("outcome") != "bad_request")
                or (r.get("op") == "lookup" and r.get("outcome") == "lookup_miss")
            )
        ),
        key=lambda r: float(r["received_at"]),
    )
    sends: Counter[str] = Counter()
    looked: set[str] = set()
    flagged: dict[str, None] = {}
    for r in events:
        key = r["idempotency_key"]
        if r["op"] == "lookup":
            looked.add(key)
            continue
        if sends[key] and key not in looked:
            flagged[key] = None
        sends[key] += 1
        looked.discard(key)
    return list(flagged)


def _faults(records: list[Mapping[str, Any]]) -> dict[str, int]:
    outcomes = Counter(r.get("outcome") for r in records if r.get("kind") == "request")
    webhooks = [r for r in records if r.get("kind") == "webhook"]
    return {
        "timeout_lost_request": outcomes["timeout_lost_request"],
        "timeout_lost_response": outcomes["timeout_lost_response"],
        "lookup_timeout": outcomes["lookup_timeout"],
        "duplicate_webhooks": len({r["event_id"] for r in webhooks if r.get("duplicate")}),
        "reordered_messages": len({r["message_id"] for r in webhooks if r.get("reordered")}),
        "late_failures": sum(
            1 for r in records if r.get("kind") == "status" and r.get("status") == "failed"
        ),
    }


def _listing(items: list[str], limit: int = 10) -> str:
    if not items:
        return "0"
    shown = ", ".join(items[:limit])
    more = f", and {len(items) - limit} more" if len(items) > limit else ""
    return f"{len(items)}: {shown}{more}"
