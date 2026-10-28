# Part 4 · One refresh per key: results

## A. The fleet: soft and hard TTL, and one refresh per key per process (60 s run)

|                                 | 10 to 60 s |
|---------------------------------|------------|
| reads served                    | 100.0%     |
| oldest data served              | 30.0 s     |
| queries sent to PostgreSQL      | 40         |
| queries that finished in time   | 40         |
| most database CPU in one second | 7.8%       |
| comment writes, 99th percentile | 8 ms       |

## B. How many queries does one refresh of p123 cost? (one key, simulated)

| API processes | queries on a cold start | reads served on a cold start | queries when it turns stale |
|---------------|-------------------------|------------------------------|-----------------------------|
| 1             | 1                       | 100.0%                       | 1                           |
| 5             | 5                       | 100.0%                       | 5                           |
| 20            | 20                      | 100.0%                       | 20                          |
| 40            | 40                      | 100.0%                       | 40                          |

## C. The real code: threads, real time, a real SQLite file, a 200 ms query

| processes | callers each | coalescing | reads served | database reads |
|-----------|--------------|------------|--------------|----------------|
| 1         | 100          | off        | 100          | 100            |
| 1         | 100          | on         | 100          | 1              |
| 20        | 100          | on         | 2,000        | 20             |

| check                                       | result                                                |
|---------------------------------------------|-------------------------------------------------------|
| p456 asked while p123's 500 ms refresh runs | answered in 81 ms                                     |
| 100 callers, a 2 s query, 500 ms deadline   | 99 gave up, the slowest after 517 ms; 1 database read |

## D. The Go versions

```
$ go run ./cmd/onekey
requests: 100, database reads: 1
$ go run ./cmd/fleet
processes: 20, requests: 2000, keys: 2, database reads: 40, shared results: 1960, took: 203ms
```
