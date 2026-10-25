# 03 · Where does each comment live? Sharding, buckets, rings and ranges

One database can't hold every comment of a busy app, so you split the comments across
several databases (**shards**). Then every request has to answer one question: **which
database owns this post?** And whenever you add, remove or lose a database, you have to
answer a second one: **what has to move, and how do you move it without losing writes?**

This challenge answers both with a real Comments API on real databases (one SQLite file
per shard), in the ways people actually run in production, all playable from one page.

![Three vnodes per database](part-3-the-ring-and-vnodes/vnodes.svg)

## The one idea to hold on to

> Hashing decides which **partition** a record belongs to. A separate **map** says which
> physical database hosts that partition right now. Moving data is a third, separate job.

Mixing these up is where the dangerous bugs live: a router that *computes* the right new
owner, pointing at a database that never *received* the rows.

## The three ways, side by side

| | Fixed buckets | Hash ring (consistent hashing) | Key ranges |
|---|---|---|---|
| **The rule** | `bucket = crc32(post_id) % 12`, then a map says which DB holds that bucket | hash the post onto a circle, the first DB token clockwise owns it | each DB owns a run of post ids, in order |
| **Add a DB** | it takes a few whole buckets, chosen on purpose | it takes the arcs just before its tokens | the busiest range splits at its median |
| **What moves** | only those buckets | only those arcs | only the upper half of one range |
| **Good at** | predictable, you choose exactly what moves | every router computes ownership from the member list alone | range scans: neighbouring keys live together |
| **Watch out** | a fixed bucket count caps how fine you can split | one token per DB is a lottery: use vnodes | new keys pile onto the last range (hotspot) |
| **Used by** | Redis Cluster (16,384 hash slots), Elasticsearch (fixed primary shards), Kafka (partitions) | Amazon Dynamo, Apache Cassandra and ScyllaDB (token rings with vnodes), memcached clients (ketama) | Google Bigtable and Spanner, Apache HBase (regions), CockroachDB (ranges), MongoDB ranged sharding |

And the one to avoid: `hash(post_id) % n`. Change `n` and almost every key changes owner
([part 1](part-1-why-modulo-breaks)).

## The seven parts

| Part | The question | What you'll see |
|---|---|---|
| [1 · Why hash % n breaks](part-1-why-modulo-breaks) | What moves when a 4th database joins 3? | `% n` moves 75% of all posts. Every other strategy moves only the new database's share. |
| [2 · Fixed buckets](part-2-fixed-buckets) | How do 12 buckets and a map handle a new database? | Post 15001 is always bucket 9. Only the map entry changes, C to D. |
| [3 · The ring and vnodes](part-3-the-ring-and-vnodes) | Which database is next clockwise, and why use many tokens? | One token each is a lottery (one DB held 98.6% of the ring). 256 vnodes each: 23% to 26.5%. |
| [4 · Key ranges](part-4-key-ranges) | What do ranges buy, and what do they cost? | A scan of 1,000 neighbouring posts asks 1 database instead of 4. All 10,000 newest posts land on 1 database. |
| [5 · Moving data safely](part-5-moving-data-safely) | Can data move while writes keep coming? What does a crash cost? | 0 of 10,000+ acknowledged writes lost during a live move. A crash with no copy: 658 posts gone, silently if you just reroute. |
| [6 · One viral post](part-6-one-viral-post) | One post gets 200,000 writes a second. Do more databases help? | No: one key is one hash is one database, at 2 databases or 32. Splitting the key into stripes spreads it, and a read then asks every stripe. |
| [7 · Stale routers and epochs](part-7-stale-routers-and-epochs) | Vnode 60 moved. One API server still has the old map. What happens to its write? | With an epoch check, 1 refusal and all 200 writes land. Without it, 200 of 200 are stranded on the old owner. A retry without a request id duplicates. |

![DB D leaves four ways](part-5-moving-data-safely/chart-leaving.svg)

## Play with it

```bash
uv sync --all-packages
uv run shardbox
```

It opens <http://127.0.0.1:8090>. Pick **hash % n**, **fixed buckets**, **hash ring**,
**ring + vnodes** or **key ranges** at the top. Then:

- **Add a database**: watch which posts move, how many rows are copied, and how long the
  fence held writes. On the teaching rings you choose the new token's position.
- **Remove** a database (its data moves first), **crash** one (it stops answering),
  **forget** a crashed one (drop it without restoring anything), or **restore** one you
  removed.
- **Send a request** for any post id and see its route: hash, bucket or position, then the
  database.
- **One viral post**: send 3,000 comments to a single post and split it 1, 8 or 32 ways: watch
  one database take everything, then the load spread.
- **A stale router**: move vnode 60 while one API server has the old map, with the epoch
  check on and off.
- The **sample posts** table shows each lesson post's owner before and after your last
  change, and the bottom panel compares how much every strategy would move if one more
  database joined right now.

Every database on that page is a real SQLite file, and every move is the same live
migration the experiments measure.

## Run the experiments

```bash
uv run challenges/03-consistent-hashing/part-1-why-modulo-breaks/modulo_breaks.py
uv run challenges/03-consistent-hashing/part-2-fixed-buckets/fixed_buckets.py
uv run challenges/03-consistent-hashing/part-3-the-ring-and-vnodes/ring_and_vnodes.py
uv run challenges/03-consistent-hashing/part-4-key-ranges/key_ranges.py
uv run challenges/03-consistent-hashing/part-5-moving-data-safely/moving_data.py
uv run challenges/03-consistent-hashing/part-6-one-viral-post/viral_post.py
uv run challenges/03-consistent-hashing/part-7-stale-routers-and-epochs/stale_routers.py
```

Each prints its tables, saves them to `results.md` next to it and redraws its pictures.
`--quick` gives a shorter run. Unlike challenges 01 and 02, this one needs no Postgres
and runs on Windows too.

## How it is built

| Piece | What it does | Code |
|---|---|---|
| Placements | `Modulo`, `FixedBuckets`, `Ring` (with vnodes and preference lists) and `Ranges`, all answering `owner(key)` | [jiffy-ring](../../packages/jiffy-ring) |
| Shards | one SQLite file per database, holding the comments it owns | [store.py](shardbox/store.py#L49) |
| Cluster | routes reads and writes, stripes hot posts, and moves data when the placement changes: capture, copy, catch up, fence, switch, clean up | [cluster.py](shardbox/cluster.py#L186) |
| Routing | the metadata record per vnode, the gateway that enforces the epoch, and the API server with its cached map | [routing.py](shardbox/routing.py) |
| Experiments | the measurements behind every table in the five parts | [experiments.py](shardbox/experiments.py) |
| API and page | the Comments API and the page above, on one port | [api.py](shardbox/api.py), [ui/](shardbox/ui) |

```
03-consistent-hashing/
  README.md                       you are here
  shardbox/                       the system: shards, cluster, API, page, experiments
  part-1-why-modulo-breaks/       README, script, results.md, charts, test
  part-2-fixed-buckets/
  part-3-the-ring-and-vnodes/
  part-4-key-ranges/
  part-5-moving-data-safely/
  part-6-one-viral-post/
  part-7-stale-routers-and-epochs/
  tests/                          the cluster, the routing pieces and the API end to end
```

## What this does not do

- **One process.** The shards are separate database files, but the router and the shards
  share a process. Real network partitions and clock skew are not modelled. Stale routers are
  (part 7), by giving each API server its own cached map and fencing at the database.
  Challenge [01](../01-zombie-worker) is the same fencing idea for a stalled worker.
- **One lock stands in for consensus.** The metadata service is a lock-protected dictionary.
  A real one is replicated (etcd, ZooKeeper) so that it survives a member failing.
- **Replicas and migration are kept apart.** With `replicas=1` every write goes to the next
  different database clockwise as well, and fail-over is a switch. Moving data *and*
  keeping every replica in step while it moves is the next level, and is refused here.
- **SQLite with `synchronous=NORMAL`.** A crashed process loses nothing; a power cut can
  lose the last moments of writes. Said plainly because it matters for the numbers.
