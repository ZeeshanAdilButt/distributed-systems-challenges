# Part 6 · Failure drills: results

## A. ElastiCache is unreachable from t = 20 s to t = 50 s (10,000 posts, warm)

|                                      | served while Valkey is down | of those, from the local cache | queries per second | served in the 10 s after |
|--------------------------------------|-----------------------------|--------------------------------|--------------------|--------------------------|
| no local cache                       | 0.61%                       | 0.0%                           | 100                | 100.0%                   |
| local cache, 2,000 entries           | 77.2%                       | 99.7%                          | 100                | 100.0%                   |
| local cache, Valkey comes back empty | 77.2%                       | 99.7%                          | 100                | 78.6%                    |

## B. PostgreSQL is down from t = 5 s to t = 160 s (p123, 2,000 reads/s, 180 s)

| window       | cache hit rate | oldest data served | refresh attempts failed or refused | reads rejected |
|--------------|----------------|--------------------|------------------------------------|----------------|
| 0 to 20 s    | 100.0%         | 40 s               | 19,458                             | 0              |
| 20 to 40 s   | 100.0%         | 60 s               | 38,841                             | 0              |
| 40 to 60 s   | 100.0%         | 80 s               | 38,884                             | 0              |
| 60 to 80 s   | 100.0%         | 100 s              | 38,992                             | 0              |
| 80 to 100 s  | 100.0%         | 120 s              | 38,890                             | 0              |
| 100 to 120 s | 0.0%           | 0 s                | 39,151                             | 40,138         |
| 120 to 140 s | 0.0%           | 0 s                | 38,800                             | 39,783         |
| 140 to 160 s | 0.0%           | 0 s                | 38,978                             | 39,991         |
| 160 to 180 s | 99.9%          | 20 s               | 15                                 | 15             |

The hit rate says 100% for 95 seconds while the data gets older every second. The age column is the one that tells the truth.
