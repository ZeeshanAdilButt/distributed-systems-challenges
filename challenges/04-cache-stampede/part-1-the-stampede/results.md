# Part 1 · The stampede: results

## A. 20,000 reads a second for p123; its cached list expires at t = 10 s (60 s run)

|                                          | before (0 to 10 s) | after it expires (10 to 60 s) |
|------------------------------------------|--------------------|-------------------------------|
| reads served                             | 100.0%             | 0.0%                          |
| 503s per second                          | 0                  | 19,809                        |
| queries sent to PostgreSQL per second    | 0                  | 19,819                        |
| queries that finished in time per second | 0                  | 0                             |
| database CPU busy                        | 3.7%               | 100.0%                        |
| queries running at once (most)           | 4                  | 541                           |
| comment writes, 99th percentile          | 3 ms               | 200 ms                        |

Recovered by the end of the run: no.

## B. The same expiry with different request deadlines (30 s runs)

| a read may wait | served after the expiry | back to 99% served after | queries sent | finished in time |
|-----------------|-------------------------|--------------------------|--------------|------------------|
| 500 ms          | 0.0%                    | never                    | 390,250      | 0                |
| 750 ms          | 96.5%                   | 2 s                      | 13,910       | 56               |
| 1000 ms         | 96.6%                   | 2 s                      | 13,910       | 500              |
| 2000 ms         | 96.8%                   | 3 s                      | 13,910       | 1,000            |

With every connection busy, 500 queries share 8 cores, so each 12 ms query takes 750 ms.

## C. The same expiry at different traffic (500 ms deadline, 30 s runs)

| reads per second | served after the expiry | back to 99% served after | most queries sent in one second |
|------------------|-------------------------|--------------------------|---------------------------------|
| 1,000            | 100.0%                  | 0 s                      | 13                              |
| 2,000            | 100.0%                  | 0 s                      | 106                             |
| 5,000            | 98.0%                   | 1 s                      | 2,019                           |
| 10,000           | 0.0%                    | never                    | 10,167                          |
| 20,000           | 0.0%                    | never                    | 20,197                          |
