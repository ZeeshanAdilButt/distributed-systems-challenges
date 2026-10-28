"""The HTTP server end to end: the page's endpoints and the real Comments API."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from stampede.api import serve


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    server = serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(url: str, body: dict[str, Any] | None = None, kind: str = "application/json") -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": kind})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_page_and_its_scenarios(base: str) -> None:
    with urllib.request.urlopen(base + "/", timeout=10) as res:
        assert b"Stampede" in res.read()
    status, body = call(base + "/api/scenarios")
    assert status == 200
    assert "expiry" in body["scenarios"]
    assert body["scenarios"]["db-outage"]["rate"] == 2_000


def test_a_simulation_run(base: str) -> None:
    status, body = call(
        base + "/api/run",
        {"scenario": "expiry", "rate": 2_000, "defenses": {"stale": True, "coalesce": True}},
    )
    assert status == 200
    assert body["after"]["served_share"] == 1.0
    assert len(body["series"]["t"]) == 60
    assert body["defenses"]["coalesce"] is True


def test_the_real_comments_api(base: str) -> None:
    status, body = call(base + "/posts/p123/comments?limit=20")
    assert status == 200
    assert body["key"] == "public-comments:p123:limit20"
    assert len(body["comments"]) == 20
    status, created = call(base + "/posts/p123/comments", {"user_id": "u7", "text": "Thanks"})
    assert status == 201
    assert created["text"] == "Thanks"


def test_a_real_burst_reads_once(base: str) -> None:
    status, body = call(base + "/api/real/burst", {"callers": 30, "coalesce": True})
    assert status == 200
    assert body["database_reads"] == 1
    assert body["served"] == 30


def test_only_json_is_accepted(base: str) -> None:
    status, _ = call(base + "/api/real/reset", {}, kind="text/plain")
    assert status == 415


def test_bad_input_is_a_400(base: str) -> None:
    status, body = call(base + "/api/run", {"scenario": "nope"})
    assert status == 400
    assert "unknown scenario" in body["error"]
