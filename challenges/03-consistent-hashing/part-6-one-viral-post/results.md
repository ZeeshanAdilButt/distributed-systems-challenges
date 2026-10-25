# Part 6 · One viral post: results

## A. 200,000 comments on ONE post (id 15001): add databases, change nothing else

| databases | busiest DB's share, 1 key | databases written, 1 key | busiest DB's share, 32 stripes | databases written, 32 stripes |
|-----------|---------------------------|--------------------------|--------------------------------|-------------------------------|
| 2         | 100.0%                    | 1                        | 50.0%                          | 2                             |
| 4         | 100.0%                    | 1                        | 34.4%                          | 4                             |
| 8         | 100.0%                    | 1                        | 31.2%                          | 7                             |
| 16        | 100.0%                    | 1                        | 37.5%                          | 6                             |
| 32        | 100.0%                    | 1                        | 34.4%                          | 7                             |

## B. Eight databases: how many stripes?

| stripes | busiest DB's share | databases written | databases a read must ask |
|---------|--------------------|-------------------|---------------------------|
| 1       | 100.0%             | 1                 | 1                         |
| 2       | 100.0%             | 1                 | 1                         |
| 4       | 100.0%             | 1                 | 1                         |
| 8       | 50.0%              | 2                 | 2                         |
| 16      | 25.0%              | 5                 | 5                         |
| 32      | 31.2%              | 7                 | 7                         |
| 64      | 28.1%              | 8                 | 8                         |
| 128     | 25.0%              | 8                 | 8                         |

The fair share is 12.5%. Stripes that differ in one character hash close together,
so a few stripes land in clumps. A few times more stripes than databases evens it out.

## C. The same with real shards (4 databases, a read merges every stripe by comment id)

| stripes | comments per database                    | read back | in order | duplicates | databases asked |
|---------|------------------------------------------|-----------|----------|------------|-----------------|
| 1       | db0: 3,000, db1: 0, db2: 0, db3: 0       | 3,000     | yes      | 0          | 1               |
| 8       | db0: 1,500, db1: 1,500, db2: 0, db3: 0   | 3,000     | yes      | 0          | 2               |
| 32      | db0: 1,031, db1: 751, db2: 468, db3: 750 | 3,000     | yes      | 0          | 4               |
