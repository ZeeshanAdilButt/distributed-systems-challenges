# jiffy-ring

Decide which database owns a key, four ways, behind one interface. Standard library only.

Every strategy answers `owner(key)`. What differs is how many keys change owner when a
database joins or leaves, and what else you get.

| | How it decides | Adding a database | Good at |
|---|---|---|---|
| `Modulo` | `nodes[crc32(key) % n]` | almost every key moves | nothing: it is the example of what to avoid |
| `FixedBuckets` | `crc32(key) % buckets` picks a bucket that never changes, and a map says which node holds it | the new node takes whole buckets, chosen on purpose | predictable, controllable moves |
| `Ring` | nodes and keys share a circle of hash positions; the first node token clockwise owns the key | the new node takes the arcs just before its tokens | every router computes ownership from the member list alone |
| `Ranges` | each node owns a contiguous run of keys, in order | the busiest range splits at its median | range scans touch one node |

```python
from jiffy_ring import Modulo, Ring, moves

nodes = ["A", "B", "C"]
ring = Ring.build(nodes, vnodes=64)  # 64 tokens per database, hashed from "A#0" ..
ring.owner(15003)  # the database that owns post 15003
grown = ring.with_node("D", vnodes=64)

moved = moves(ring, grown, range(10_000, 110_000))
len(moved) / 100_000  # 0.30: D's share, give or take hash luck (fair: 0.25)
{m.after for m in moved}  # {"D"}: nothing moved between A, B and C

moves(Modulo(nodes), Modulo(nodes).with_node("D"), range(10_000, 110_000))  # 75% of them
```

## The ring

```python
lesson = Ring([(40, "A"), (70, "B"), (90, "C")], space=100)  # a tiny 0..99 ring to read by hand
lesson.owner_of_position(49)  # "B": the next token clockwise
lesson.arcs()  # who owns which positions, including the wrap past 99
lesson.ownership()  # {"A": 0.5, "B": 0.3, "C": 0.2}
after = lesson.with_tokens([(60, "D")])
lesson.taken_by(after, "D")  # [Arc(41, 60, "B")]: the data that must move, and from whom
lesson.token_at(57)  # 70: the vnode, as a number
```

- `Ring.build(nodes, vnodes=N)` places each node `N` times at `crc32("node#i")`, so every router
  computes the same ring from the member list.
- `ring.preference(key, n)` returns the first `n` **different** nodes clockwise: the owner,
  then where its replicas go. Several tokens of one node count once, because vnodes on one
  machine die together.
- `changed_arcs(old, new)` lists every arc whose owner differs between two rings.

## Hashing is stable, on purpose

`position(key)` is `crc32(str(key)) % space`. It is the same on every machine and in every
run, unlike Python's built-in `hash()`, which is salted per process. CRC32 is used because it
lets a lesson be checked by hand (`crc32(b"15003") % 100` is 49). It is not the best mixer:
keys that differ in one character land close together, which part 6 of challenge 03 measures.
A production system would use murmur, xxhash or SHA-256.

## What it does not do

It does not move data, store anything, or replicate. It only says where a key *belongs*.
Moving the rows is a separate, harder job: see
[challenge 03](../../challenges/03-consistent-hashing), part 5.
