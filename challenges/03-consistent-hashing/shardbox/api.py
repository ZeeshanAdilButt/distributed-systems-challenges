"""The Comments API and the page to play with it, on one local port.

    POST /posts/{id}/comments     write a comment, routed to the shard that owns the post
    GET  /posts/{id}/comments     read them back, with the route that was taken

    GET  /api/state               everything the page draws
    POST /api/reset               {"strategy": ..., "preset": ...}  start over with seed data
    POST /api/nodes               {"name"?: "D", "position"?: 60}   add a database (live move)
    POST /api/nodes/{name}/remove graceful: move its data first
    POST /api/nodes/{name}/crash  the process dies; the router still points at it
    POST /api/nodes/{name}/forget drop it from the placement without restoring anything
    POST /api/nodes/{name}/restore put a removed database back at its old ring tokens
    POST /api/burst               {"count": 200} write that many comments at random posts
    POST /api/hot                 {"stripes": 8}  3,000 comments on one viral post, striped
    POST /api/stale               {"fenced": true}  move a vnode while one API has the old map

Standard library only. Every shard is a real SQLite file in a temporary folder.
"""

from __future__ import annotations

import json
import random
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jiffy_ring import FixedBuckets, Modulo, Placement, Ranges, Ring, moves, position
from shardbox import experiments as ex
from shardbox.cluster import Cluster, Migration
from shardbox.store import Down

UI = Path(__file__).with_name("ui")
SAMPLES = (15001, 15002, 15003, 15008, 15009, 15010, 15012, 15055)
NAMES = "ABCDEFGHIJ"
PRESETS = {
    "modulo": ("modulo", "hash(post_id) % n"),
    "buckets": ("buckets", "12 fixed buckets and a partition map"),
    "ring-lesson": ("ring", "a 0..99 ring: A at 40, B at 70, C at 90"),
    "ring-vnodes-lesson": ("ring", "a 0..99 ring with 3 vnodes each"),
    "ring-hashed": ("ring", "a real 2^32 ring, 1 hashed token each"),
    "ring-vnodes": ("ring", f"a real 2^32 ring, {ex.VNODES} vnodes each"),
    "ranges": ("ranges", "key ranges, split at the median when a database joins"),
}


def placement_for(preset: str) -> Placement:
    nodes = ["A", "B", "C"]
    if preset == "modulo":
        return Modulo(nodes)
    if preset == "buckets":
        return FixedBuckets.spread(nodes, ex.BUCKETS)
    if preset == "ring-lesson":
        return ex.lesson_ring()
    if preset == "ring-vnodes-lesson":
        return ex.vnode_lesson_ring()
    if preset == "ring-hashed":
        return Ring.build(nodes, vnodes=1)
    if preset == "ring-vnodes":
        return Ring.build(nodes, vnodes=ex.VNODES)
    if preset == "ranges":
        return Ranges.even(nodes, ex.POSTS.stop, ex.POSTS.start)
    raise ValueError(f"unknown preset {preset!r}")


class Lab:
    """One live cluster plus what the page needs to explain it."""

    cluster: Cluster

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._folder = tempfile.TemporaryDirectory(prefix="shardbox-", ignore_cleanup_errors=True)
        self._generation = 0
        self._started = False
        self.reset("ring-lesson")

    def reset(self, preset: str, seed_comments: int = 600) -> None:
        with self._lock:
            if self._started:
                self.cluster.close()
            self._started = True
            self._generation += 1
            folder = Path(self._folder.name) / f"run-{self._generation}"
            self.preset = preset
            self.cluster = Cluster(folder, placement_for(preset))
            self.previous: Placement | None = None
            self.last: Migration | None = None
            self.last_action = f"started with {PRESETS[preset][1]}"
            self.events: list[str] = []
            self.crashed: set[str] = set()
            self.removed: dict[str, list[int]] = {}  # name -> its ring tokens, for Restore
            self.badge = ""
            rng = random.Random(1)
            for post in SAMPLES:
                self.cluster.write(post, user_id=42, text="lesson comment")
            for _ in range(seed_comments):
                self.cluster.write(rng.randrange(10_000, 110_000), user_id=1, text="seed")

    # ------------------------------------------------------------- membership

    def _grown(self, name: str, at: int | None) -> Placement:
        p = self.cluster.placement
        if isinstance(p, Ring):
            if at is not None:
                return p.with_tokens([(at % p.space, name)])
            vnodes = len(p.tokens) // max(1, len(p.nodes))
            return p.with_node(name, vnodes=vnodes)
        if isinstance(p, Modulo):
            return p.with_node(name)
        if isinstance(p, FixedBuckets):
            return p.with_node(name)[0]
        if isinstance(p, Ranges):
            return p.with_node(name, self._keys())[0]
        raise TypeError(type(p).__name__)

    def _keys(self) -> list[int]:
        return [c.post_id for s in self.cluster.shards.values() if s.up for c in s.all_rows()]

    def _shrunk(self, name: str) -> Placement:
        p = self.cluster.placement
        if isinstance(p, (Ring, Modulo, FixedBuckets, Ranges)):
            return p.without_node(name)
        raise TypeError(type(p).__name__)

    def add(self, name: str | None, at: int | None) -> None:
        with self._lock:
            name = name or next(n for n in NAMES if n not in self.cluster.shards)
            new = self._grown(name, at)
            self.previous = self.cluster.placement
            self.events = []
            self.cluster.on_step = self.events.append
            self.last = self.cluster.add_node(name, new)
            self.removed.pop(name, None)
            self.badge = f"DB {name} added"
            self.last_action = f"added DB {name}" + (f" at position {at}" if at is not None else "")

    def restore(self, name: str) -> None:
        """Put a removed database back where it was on the ring, with a live migration."""
        with self._lock:
            p = self.cluster.placement
            tokens = self.removed.get(name)
            if not isinstance(p, Ring) or tokens is None:
                raise ValueError(f"nothing to restore for DB {name}")
            self.previous = p
            self.events = []
            self.cluster.on_step = self.events.append
            self.last = self.cluster.add_node(name, p.with_tokens([(t, name) for t in tokens]))
            del self.removed[name]
            self.badge = f"DB {name} restored"
            self.last_action = f"restored DB {name} at its old tokens and moved its data back"

    def _remember(self, name: str) -> None:
        p = self.cluster.placement
        if isinstance(p, Ring):
            self.removed[name] = [t for t, n in p.tokens if n == name]

    def remove(self, name: str) -> None:
        with self._lock:
            if name in self.crashed:
                raise ValueError(f"DB {name} crashed: its data cannot be moved. Try forget.")
            self.previous = self.cluster.placement
            self.events = []
            self.cluster.on_step = self.events.append
            self._remember(name)
            self.last = self.cluster.remove_node(name, self._shrunk(name))
            self.badge = f"DB {name} removed"
            self.last_action = f"removed DB {name} gracefully (moved its data first)"

    def crash(self, name: str) -> None:
        with self._lock:
            self.cluster.crash(name)
            self.crashed.add(name)
            self.badge = f"DB {name} crashed"
            self.last_action = f"DB {name} crashed. The router still sends its posts there."

    def forget(self, name: str) -> None:
        with self._lock:
            self.previous = self.cluster.placement
            self._remember(name)
            self.cluster.forget_node(name, self._shrunk(name))
            self.badge = f"DB {name} forgotten"
            self.crashed.discard(name)
            self.last = None
            self.last_action = (
                f"dropped DB {name} from the placement without restoring its data. Its posts "
                "now route to databases that never had them."
            )

    def hot(self, stripes: int) -> dict[str, Any]:
        """3,000 comments on one viral post on a throwaway cluster with the live placement
        (same strategy, same tokens, same databases), so the page's own data is left alone."""
        stripes = max(1, min(stripes, 256))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            cluster = Cluster(Path(folder), self.cluster.placement)  # the live shape
            out: dict[str, Any] = {"post": ex.VIRAL, "stripes": stripes}
            try:
                cluster.stripe(ex.VIRAL, stripes)
            except ValueError as exc:
                out["note"] = str(exc)
                cluster.close()
                return out
            for i in range(3_000):
                cluster.write(ex.VIRAL, i % 50, "first!")
            comments, asked = cluster.read(ex.VIRAL)
            rows = {n: len(s.for_post(ex.VIRAL)) for n, s in sorted(cluster.shards.items())}
            out.update(
                rows=rows,
                read_back=len(comments),
                asked=asked.split(","),
                busiest=max(rows.values()) / 3_000,
            )
            cluster.close()
        return out

    def stale(self, fenced: bool) -> dict[str, Any]:
        run = ex.stale_router(fenced, writes=200)
        return {
            "fenced": run.fenced,
            "writes": run.writes,
            "refused": run.refused,
            "refreshes": run.refreshes,
            "readable": run.readable,
            "stranded": run.stranded_on_old_owner,
            "epoch": [run.epoch_before, run.epoch_after],
            "steps": run.steps,
        }

    def burst(self, count: int) -> int:
        rng = random.Random()
        ok = 0
        for _ in range(count):
            try:
                self.cluster.write(rng.randrange(10_000, 110_000), user_id=7, text="burst")
                ok += 1
            except Down:
                pass
        return ok

    # ------------------------------------------------------------------ state

    def route(self, post_id: int) -> dict[str, Any]:
        p = self.cluster.placement
        out: dict[str, Any] = {"post_id": post_id, "owner": p.owner(post_id)}
        if isinstance(p, Ring):
            out["position"] = position(post_id, p.space)
            out["rule"] = "first token clockwise from the post's position"
        elif isinstance(p, FixedBuckets):
            out["bucket"] = p.bucket(post_id)
            out["rule"] = f"bucket = crc32 % {p.buckets}, then the partition map"
        elif isinstance(p, Modulo):
            out["hash"] = position(post_id)
            out["rule"] = f"crc32 % {len(p.nodes)}"
        else:
            out["rule"] = "the range whose start is the largest one at or below the post id"
        return out

    def state(self) -> dict[str, Any]:
        with self._lock:
            p = self.cluster.placement
            counts = {}
            for name, shard in sorted(self.cluster.shards.items()):
                counts[name] = shard.count()
            total = sum(counts.values()) or 1
            s: dict[str, Any] = {
                "preset": self.preset,
                "presets": {k: v[1] for k, v in PRESETS.items()},
                "strategy": PRESETS[self.preset][0],
                "nodes": [
                    {
                        "name": n,
                        "rows": counts[n],
                        "share": counts[n] / total,
                        "up": n not in self.crashed,
                    }
                    for n in counts
                ],
                "last_action": self.last_action,
                "steps": list(self.events),
                "samples": [self._sample(post) for post in SAMPLES],
                "next_join": self._next_join(),
                "badge": self.badge,
                "removed": sorted(self.removed),
            }
            if self.last is not None:
                s["migration"] = {
                    "copied": self.last.copied,
                    "captured": self.last.captured,
                    "fenced_ms": round(self.last.fenced_ms, 2),
                    "dropped": self.last.dropped,
                    "moved": dict(self.last.moved),
                }
            if isinstance(p, Ring):
                ghosts = [[t, n] for n, ts in sorted(self.removed.items()) for t in ts]
                s["ring"] = {
                    "space": p.space,
                    "tokens": [[t, n] for t, n in p.tokens],
                    "ghosts": ghosts,
                }
            elif isinstance(p, FixedBuckets):
                s["buckets"] = {str(b): n for b, n in sorted(p.assignment.items())}
            elif isinstance(p, Ranges):
                s["ranges"] = {"top": ex.POSTS.stop, "starts": [[a, n] for a, n in p.starts]}
            else:
                s["modulo"] = {"n": len(p.nodes), "nodes": p.nodes}
            return s

    def _sample(self, post: int) -> dict[str, Any]:
        r = self.route(post)
        r["before"] = self.previous.owner(post) if self.previous is not None else None
        try:
            r["comments"] = len(self.cluster.read(post)[0])
        except Down:
            r["comments"] = None
        return r

    def _next_join(self) -> dict[str, float]:
        """If one more database joined now: share of keys each strategy would move, for
        this many databases. Shown next to the live cluster for comparison."""
        nodes = sorted(self.cluster.shards)
        keys = range(10_000, 30_000)
        out = {}
        for strategy in ex.STRATEGIES:
            before = ex.build(strategy, nodes)
            after = ex.grow(strategy, before, "new", keys)
            out[strategy] = round(len(moves(before, after, keys)) / len(keys), 4)
        return out


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
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path in ("/app.js", "/style.css"):
            kind = "text/javascript" if path.endswith(".js") else "text/css"
            return self._file(path[1:], kind)
        if path == "/api/state":
            return self._json(200, self.lab.state())
        m = re.fullmatch(r"/posts/(\d+)/comments", path)
        if m:
            post = int(m.group(1))
            try:
                comments, owner = self.lab.cluster.read(post)
            except Down as exc:
                return self._json(503, {"error": str(exc), **self.lab.route(post)})
            return self._json(
                200,
                {
                    **self.lab.route(post),
                    "served_by": owner,
                    "comments": [c.__dict__ for c in comments],
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        try:
            body = self._body()
            m = re.fullmatch(r"/posts/(\d+)/comments", path)
            if m:
                post = int(m.group(1))
                comment, owner = self.lab.cluster.write(
                    post, int(body.get("user_id", 1)), str(body.get("text", ""))[:500]
                )
                return self._json(
                    201, {**self.lab.route(post), "stored_on": owner, **comment.__dict__}
                )
            if path == "/api/reset":
                self.lab.reset(str(body.get("preset", "ring-lesson")))
            elif path == "/api/nodes":
                at = body.get("position")
                self.lab.add(body.get("name") or None, int(at) if at not in (None, "") else None)
            elif path == "/api/hot":
                return self._json(200, self.lab.hot(int(body.get("stripes", 1))))
            elif path == "/api/stale":
                return self._json(200, self.lab.stale(bool(body.get("fenced", True))))
            elif path == "/api/burst":
                return self._json(
                    200, {"written": self.lab.burst(min(int(body.get("count", 200)), 5000))}
                )
            elif m := re.fullmatch(r"/api/nodes/([A-Z])/(remove|crash|forget|restore)", path):
                getattr(self.lab, m.group(2))(m.group(1))
            else:
                return self._json(404, {"error": "not found"})
        except Down as exc:
            return self._json(503, {"error": str(exc)})
        except PermissionError as exc:
            return self._json(415, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, self.lab.state())

    def _file(self, name: str, kind: str) -> None:
        data = (UI / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 8090) -> ThreadingHTTPServer:
    Handler.lab = Lab()
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
