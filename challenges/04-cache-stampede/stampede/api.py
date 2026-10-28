"""The page, the simulator behind it, and the real Comments API, on one local port.

    GET  /posts/{id}/comments?limit=20   the real read path (local cache, shared cache,
                                         coalescing, budget, a slow SQLite query)
    POST /posts/{id}/comments            {"user_id": "u1", "text": "..."}  returns the comment

    GET  /api/scenarios                  the scenarios and defences the page offers
    POST /api/run                        {"scenario": "expiry", "defenses": {...}, "rate": ...}
                                         simulate the fleet and return every series
    POST /api/real/burst                 {"callers": 100, "coalesce": true}  a cold post,
                                         that many threads at once, count database reads
    POST /api/real/reset                 empty the real caches

Standard library only.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from stampede import experiments as ex
from stampede.real import CommentsAPI, CommentsDB, Settings, SharedCache, burst
from stampede.sim import Config, Defenses, run

UI = Path(__file__).with_name("ui")
LIMITS = {"rate": (500, 40_000), "instances": (2, 60), "deadline": (0.1, 3.0)}
DEFENSE_NAMES = ("stale", "coalesce", "budget", "local", "fleet_budget", "preserve_age")


def _clamp(name: str, value: object) -> float:
    lo, hi = LIMITS[name]
    return max(lo, min(hi, float(value)))  # type: ignore[arg-type]


class Lab:
    def __init__(self) -> None:
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._tmp = tempfile.TemporaryDirectory(prefix="stampede-", ignore_cleanup_errors=True)
        self.db = CommentsDB(Path(self._tmp.name) / "comments.db")
        for post in ("p123", "p456"):
            self.db.seed(post, 30)
        self.shared = SharedCache(time.time)
        self.api = CommentsAPI(self.db, self.shared)

    # -- the simulator

    def config(self, body: dict[str, Any]) -> tuple[Config, ex.Scenario]:
        name = str(body.get("scenario", "expiry"))
        if name not in ex.SCENARIOS:
            raise ValueError(f"unknown scenario {name}")
        sc = ex.SCENARIOS[name]
        wanted = body.get("defenses") or {}
        defenses = Defenses(
            **{k: bool(wanted.get(k, getattr(Defenses(), k))) for k in DEFENSE_NAMES}
        )
        changes: dict[str, Any] = {"defenses": defenses}
        if "rate" in body:
            changes["rate"] = _clamp("rate", body["rate"])
        if "instances" in body:
            changes["instances"] = int(_clamp("instances", body["instances"]))
        if "deadline" in body:
            changes["deadline"] = _clamp("deadline", body["deadline"])
        return replace(sc.config, **changes), sc

    def simulate(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg, sc = self.config(body)
        key = repr(cfg)
        with self._lock:
            if key in self._runs:
                self._runs.move_to_end(key)
                return self._runs[key]
        t0 = time.perf_counter()
        r = run(cfg)
        result = {
            "config": {k: v for k, v in asdict(cfg).items() if k not in ("events", "defenses")},
            "defenses": asdict(cfg.defenses),
            "scenario": {
                "title": sc.title,
                "story": sc.story,
                "marks": sc.marks,
                "bands": sc.bands,
                "watch_from": sc.watch_from,
            },
            "series": r.series.as_dict(),
            "before": r.window(0, sc.watch_from) if sc.watch_from else None,
            "after": r.window(sc.watch_from),
            "recovered_at": r.recovered_at(sc.watch_from),
            "safe_rate": ex.SAFE_RATE,
            "seconds_to_simulate": round(time.perf_counter() - t0, 2),
        }
        with self._lock:
            self._runs[key] = result
            while len(self._runs) > 24:
                self._runs.popitem(last=False)
        return result

    def scenarios(self) -> dict[str, Any]:
        return {
            "scenarios": {
                k: {
                    "title": s.title,
                    "story": s.story,
                    "seconds": s.config.seconds,
                    "rate": s.config.rate,
                    "instances": s.config.instances,
                    "deadline": s.config.deadline,
                }
                for k, s in ex.SCENARIOS.items()
            },
            "layers": {k: label for k, (label, _) in ex.LAYERS.items()},
            "defaults": {"rate": 20_000, "instances": 20, "deadline": 0.5},
        }

    # -- the real code

    def real_burst(self, callers: int, coalesce: bool) -> dict[str, Any]:
        callers = max(1, min(callers, 300))
        self.shared.clear()
        api = CommentsAPI(self.db, self.shared, Settings(coalesce=coalesce, budget=False))
        key_post = "p456"
        before = self.db.reads()
        t0 = time.perf_counter()
        replies = burst(api, key_post, callers)
        seconds = time.perf_counter() - t0
        api.close()
        self.shared.clear()
        return {
            "callers": callers,
            "coalesce": coalesce,
            "database_reads": self.db.reads() - before,
            "served": sum(r.status == 200 for r in replies),
            "seconds": round(seconds, 3),
        }

    def reset_real(self) -> None:
        self.shared.clear()
        self.api.close()
        self.api = CommentsAPI(self.db, self.shared)


class Handler(BaseHTTPRequestHandler):
    lab: Lab

    def log_message(self, *args: object) -> None:  # keep the terminal quiet
        pass

    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise PermissionError("JSON only")  # a page on another site cannot post JSON here
        data = json.loads(self.rfile.read(length))
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path in ("/app.js", "/style.css"):
            kind = "text/javascript" if path.endswith(".js") else "text/css"
            return self._file(path[1:], kind)
        if path == "/api/scenarios":
            return self._json(200, self.lab.scenarios())
        m = re.fullmatch(r"/posts/(p\d+)/comments", path)
        if m:
            limit = int(parse_qs(url.query).get("limit", ["20"])[0])
            reply = self.lab.api.get(m.group(1), max(1, min(limit, 100)))
            return self._json(
                reply.status,
                {
                    "outcome": reply.outcome,
                    "age_seconds": reply.age,
                    "database_reads": self.lab.db.reads(),
                    **reply.body,
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            body = self._body()
            m = re.fullmatch(r"/posts/(p\d+)/comments", path)
            if m:
                reply = self.lab.api.create(
                    m.group(1), str(body.get("user_id", "u1"))[:40], str(body.get("text", ""))[:500]
                )
                return self._json(reply.status, reply.body)
            if path == "/api/run":
                return self._json(200, self.lab.simulate(body))
            if path == "/api/real/burst":
                return self._json(
                    200,
                    self.lab.real_burst(
                        int(body.get("callers", 100)), bool(body.get("coalesce", True))
                    ),
                )
            if path == "/api/real/reset":
                self.lab.reset_real()
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(415, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": str(exc)})

    def _file(self, name: str, kind: str) -> None:
        data = (UI / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 8091) -> ThreadingHTTPServer:
    Handler.lab = Lab()
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
