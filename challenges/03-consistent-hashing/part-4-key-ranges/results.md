# Part 4 · Key ranges: results

## A. Read posts 15,000 to 15,999 (a range scan): how many databases must answer?

| strategy             | databases asked (of 4) |
|----------------------|------------------------|
| 12 fixed buckets     | 4                      |
| ring, 64 vnodes each | 4                      |
| key ranges           | 1                      |

## B. Where do the 10,000 newest posts go? (new posts get the next id)

| strategy             | DB A  | DB B  | DB C  | DB D   |
|----------------------|-------|-------|-------|--------|
| 12 fixed buckets     | 23.8% | 25.7% | 25.2% | 25.2%  |
| ring, 64 vnodes each | 26.7% | 16.7% | 26.1% | 30.5%  |
| key ranges           | 0.0%  | 0.0%  | 0.0%  | 100.0% |

## C. DB E joins: the busiest range splits at its median

|        | ranges (first post id: owner)                    |
|--------|--------------------------------------------------|
| before | 0: A, 35,000: B, 60,000: C, 85,000: D            |
| after  | 0: A, 22,500: E, 35,000: B, 60,000: C, 85,000: D |

Split at 22,500. Posts moved: 12.5%, all from DB A (12,500 posts) to DB E.
