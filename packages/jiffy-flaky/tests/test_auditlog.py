"""The log writer: file order matches memory order, and shared fsyncs lose nothing."""

from __future__ import annotations

import threading
from pathlib import Path

from jiffy_flaky import read_log
from jiffy_flaky.auditlog import AuditLog


def test_concurrent_appends_land_in_the_file_in_order(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    audit_log = AuditLog(path, fsync=True)

    def write(thread: int) -> None:
        for i in range(25):
            audit_log.append("effect", thread=thread, i=i, wait_for_fsync=i % 2 == 0)

    threads = [threading.Thread(target=write, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    audit_log.close()
    assert read_log(path) == audit_log.records()
    assert len(audit_log.records()) == 200


def test_records_after_close_stay_in_memory_only(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    audit_log = AuditLog(path)
    audit_log.append("status", wait_for_fsync=False, status="queued")
    audit_log.close()
    audit_log.append("status", status="sent")
    assert [r["status"] for r in read_log(path)] == ["queued"]
    assert [r["status"] for r in audit_log.records()] == ["queued", "sent"]


def test_without_a_path_the_log_is_memory_only() -> None:
    audit_log = AuditLog()
    record = audit_log.append("request", op="send")
    assert record["kind"] == "request"
    assert isinstance(record["at"], float)
    assert audit_log.records() == [record]
