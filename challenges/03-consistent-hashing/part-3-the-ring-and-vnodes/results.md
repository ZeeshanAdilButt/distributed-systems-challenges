# Part 3 · The ring and virtual nodes: results

## A. One token each: A at 40, B at 70, C at 90 (ring 0..99)

| database | owns positions       | share |
|----------|----------------------|-------|
| DB A     | 91 to 99 and 0 to 40 | 50.0% |
| DB B     | 41 to 70             | 30.0% |
| DB C     | 71 to 90             | 20.0% |

DB D joins at 60, with one real comment per post:

| post  | position | before | after        |
|-------|----------|--------|--------------|
| 15001 | 25       | DB A   | DB A         |
| 15002 | 79       | DB C   | DB C         |
| 15003 | 49       | DB B   | DB D (moved) |
| 15008 | 57       | DB B   | DB D (moved) |
| 15009 | 31       | DB A   | DB A         |
| 15010 | 62       | DB B   | DB B         |

Rows copied: 2 (B->D: 2). Rows per database before {'A': 2, 'B': 3, 'C': 1}, after {'A': 2, 'B': 1, 'C': 1, 'D': 2}.
Quick check: position 65 belongs to DB B.

## B. Three vnodes each: A at 10, 50, 80; B at 20, 60, 90; C at 30, 40, 70

DB B is removed gracefully:

| post  | position | before | after        |
|-------|----------|--------|--------------|
| 15001 | 25       | DB C   | DB C         |
| 15002 | 79       | DB A   | DB A         |
| 15003 | 49       | DB A   | DB A         |
| 15008 | 57       | DB B   | DB C (moved) |
| 15012 | 14       | DB B   | DB C (moved) |
| 15055 | 89       | DB B   | DB A (moved) |

Rows copied: 3 (B->A: 1, B->C: 2). DB B's data went to two databases, not one.

## C. Four databases on a real 2^32 ring: how even is it?

| vnodes each | busiest DB | quietest DB | spread (stdev) | DBs giving data to a 5th | biggest single handoff |
|-------------|------------|-------------|----------------|--------------------------|------------------------|
| 1           | 98.6%      | 0.20%       | 42.5%          | 1                        | 61.5%                  |
| 2           | 56.7%      | 0.49%       | 24.7%          | 1                        | 19.5%                  |
| 4           | 42.6%      | 9.9%        | 12.3%          | 2                        | 20.5%                  |
| 8           | 51.5%      | 2.0%        | 22.1%          | 2                        | 20.2%                  |
| 16          | 29.8%      | 19.1%       | 4.2%           | 4                        | 4.5%                   |
| 64          | 29.4%      | 19.2%       | 4.1%           | 4                        | 1.1%                   |
| 256         | 26.5%      | 23.0%       | 1.3%           | 4                        | 0.32%                  |
