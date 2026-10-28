# Part 2 · Jitter is not enough: results

## A. 10,000 posts cached at the same moment, all due at t = 10 s (20,000 reads/s)

| TTL jitter | queries in total | most in any one second | most in any 100 ms |
|------------|------------------|------------------------|--------------------|
| ±0%        | 10,243           | 8,899                  | 1,864              |
| ±10%       | 10,244           | 1,752                  | 206                |
| ±20%       | 10,289           | 908                    | 119                |

## B. One post, p123, with the same jitter

| TTL jitter | queries in total | most in any one second | most in any 100 ms |
|------------|------------------|------------------------|--------------------|
| ±0%        | 263              | 263                    | 263                |
| ±10%       | 227              | 227                    | 227                |
| ±20%       | 257              | 257                    | 257                |

## C. And against the real 8-core database

| posts        | TTL jitter | reads served, 16 to 30 s | back to 99% | database CPU |
|--------------|------------|--------------------------|-------------|--------------|
| 10,000 posts | ±0%        | 0.0%                     | never       | 100.0%       |
| 10,000 posts | ±20%       | 5.6%                     | never       | 100.0%       |
| one post     | ±0%        | 0.0%                     | never       | 100.0%       |
| one post     | ±20%       | 0.0%                     | never       | 100.0%       |
