"""The command line: audit exit codes in-process, and serve as a real child process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jiffy_flaky import read_log
from jiffy_flaky.cli import main
from jiffy_flaky.testing import FlakyClient, request_json


def write_log(path: Path, actions: list[str]) -> Path:
    lines = [
        json.dumps(
            {
                "kind": "effect",
                "at": 0.0,
                "message_id": f"m{i}",
                "idempotency_key": f"k{i}",
                "metadata": {"action_id": action},
                "to": "+15550100",
            }
        )
        for i, action in enumerate(actions)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_audit_exits_0_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = write_log(tmp_path / "log.jsonl", ["a1", "a2"])
    assert main(["audit", str(log)]) == 0
    assert "OK" in capsys.readouterr().out


def test_audit_exits_1_on_a_duplicate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = write_log(tmp_path / "log.jsonl", ["a1", "a1"])
    assert main(["audit", str(log), "--json"]) == 1
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert report["duplicates"] == {"a1": 2}
    assert report["ok"] is False


def test_audit_exits_1_on_a_missing_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = write_log(tmp_path / "log.jsonl", ["a1"])
    ids = tmp_path / "ids.txt"
    ids.write_text("a1\na2\n\n", encoding="utf-8")
    assert main(["audit", str(log), "--expect-ids", str(ids)]) == 1
    assert "1: a2" in capsys.readouterr().out


def test_audit_groups_by_another_field(tmp_path: Path) -> None:
    log = write_log(tmp_path / "log.jsonl", ["a1", "a2"])
    assert main(["audit", str(log)]) == 0
    assert main(["audit", str(log), "--group-by", "order_id"]) == 1


def test_audit_exits_2_when_the_log_is_missing(tmp_path: Path) -> None:
    assert main(["audit", str(tmp_path / "nope.jsonl")]) == 2


def test_serve_runs_until_sigterm(tmp_path: Path) -> None:
    log = tmp_path / "serve.jsonl"
    env = {**os.environ, "FLAKY_SEND_TIMEOUT_RATE": "0", "FLAKY_SEND_LATENCY": "0"}
    process = subprocess.Popen(
        [sys.executable, "-m", "jiffy_flaky", "serve", "--log", str(log), "--seed", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line.startswith("jiffy-flaky listening on http://127.0.0.1:"), line
        url = line.split()[-1]
        assert request_json("GET", f"{url}/_health").body == {"ok": True}
        assert FlakyClient(url).send("k1", metadata={"action_id": "a1"}).status == 201
    finally:
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert [r["kind"] for r in read_log(log)][:3] == ["effect", "status", "request"]


def test_serve_refuses_a_bad_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["serve", "--send-timeout-rate", "lots"])
    assert exited.value.code == 2
    assert "send-timeout-rate" in capsys.readouterr().err
