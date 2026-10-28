# Part 5 · A budget for the database: results

## A. A complete cold start: 10,000 posts, every cache empty, 20,000 reads/s (40 s runs)

|                     | reads served, each 10 s       | queries sent per second | most in one second | finished in time | database CPU | writes, 99th percentile |
|---------------------|-------------------------------|-------------------------|--------------------|------------------|--------------|-------------------------|
| one refresh per key | 0.0% / 0.0% / 0.0% / 0.0%     | 10,666                  | 10,995             | 0                | 100.0%       | 199 ms                  |
| + refresh budget    | 58.1% / 73.4% / 80.5% / 84.4% | 102                     | 180                | 4,080            | 19.0%        | 16 ms                   |

With the budget, 207,127 reads got an immediate 503 instead of a query, and 222,184 refreshes were refused in all.

## B. The fleet doubles from 20 to 40 processes at t = 10 s, mid cold start

| refresh budget               | queries per second, 20 processes | queries per second, 40 processes | database CPU after |
|------------------------------|----------------------------------|----------------------------------|--------------------|
| 5/s per process, fixed       | 100                              | 200                              | 33.7%              |
| 100/s for the fleet, divided | 100                              | 100                              | 18.8%              |
