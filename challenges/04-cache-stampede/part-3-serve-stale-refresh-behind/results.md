# Part 3 · Serve stale, refresh behind: results

## A. p123 expires at t = 10 s; soft TTL 30 s, hard TTL 120 s (130 s runs)

|                   | served, 10 to 100 s | served after 100 s | oldest served  | queries sent per second | finished in time | database CPU |
|-------------------|---------------------|--------------------|----------------|-------------------------|------------------|--------------|
| plain 30 s TTL    | 0.0%                | 0.0%               | nothing served | 19,908                  | 0                | 100.0%       |
| soft and hard TTL | 100.0%              | 0.0%               | 120 s          | 19,908                  | 0                | 100.0%       |

Every stale read also started its own background refresh. None of them finished.

## B. PostgreSQL is down from t = 5 s; a deploy restarts every API process at t = 60 s

|                              | last response served at | oldest data served | oldest age the cache believed |
|------------------------------|-------------------------|--------------------|-------------------------------|
| timestamps kept on copy      | 100 s                   | 120 s              | 120 s                         |
| copy re-stamped as just read | 181 s                   | 200 s              | 120 s                         |

## C. Age from the read, not from the arrival (the real code, a slow query)

| query took | age of the entry the moment it landed |
|------------|---------------------------------------|
| 1.0 s      | 1.02 s                                |
