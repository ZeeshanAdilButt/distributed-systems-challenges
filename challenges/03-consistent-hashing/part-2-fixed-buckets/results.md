# Part 2 · Fixed buckets: results

## A. Where post 15001 goes

| step                       | value               |
|----------------------------|---------------------|
| crc32("15001")             | 929,414,625         |
| % 12                       | bucket 9            |
| partition map, before DB D | bucket 9 is on DB C |
| partition map, after DB D  | bucket 9 is on DB D |

## B. DB D joins and takes buckets 1, 5, 9, with real rows

| database | buckets before | buckets after | rows before | rows after | share after |
|----------|----------------|---------------|-------------|------------|-------------|
| DB A     | 0, 1, 2, 3     | 0, 2, 3       | 980         | 700        | 23.3%       |
| DB B     | 4, 5, 6, 7     | 4, 6, 7       | 1006        | 773        | 25.8%       |
| DB C     | 8, 9, 10, 11   | 8, 10, 11     | 1014        | 747        | 24.9%       |
| DB D     | -              | 1, 5, 9       | 0           | 780        | 26.0%       |

Rows copied: 780 (A->D: 280, B->D: 233, C->D: 267). Every one went to DB D.
