# Part 5 · Moving data safely: results

## A. DB D joins while 8 writers keep writing (4 s per run)

| strategy             | writes acked | read back once | lost | duplicated | rows copied | writes during the move | captured and replayed | fence  | slowest write |
|----------------------|--------------|----------------|------|------------|-------------|------------------------|-----------------------|--------|---------------|
| 12 fixed buckets     | 11,570       | 11,570         | 0    | 0          | 2,120       | 960                    | 183                   | 2.3 ms | 531 ms        |
| ring, 64 vnodes each | 9,762        | 9,762          | 0    | 0          | 2,185       | 766                    | 162                   | 3.4 ms | 576 ms        |
| key ranges           | 14,506       | 14,506         | 0    | 0          | 1,345       | 745                    | 59                    | 0.2 ms | 960 ms        |

The steps of the last run:

1. capture: log every write to a post whose owner is changing
1. copy: 1345 rows to their new shards
1. catch up: replayed 58 captured writes while writes kept flowing
1. fence: held moving writes 0.2 ms, replayed 59
1. switch: every request now uses the new placement
1. cleanup: dropped 1395 moved rows from their old shards

## B. DB D leaves: what happens to the 658 posts it held

| how DB D left                        | still readable | error (unavailable) | came back empty |
|--------------------------------------|----------------|---------------------|-----------------|
| graceful removal                     | 658            | 0                   | 0               |
| crash                                | 0              | 658                 | 0               |
| crash, then dropped from the ring    | 0              | 0                   | 658             |
| crash with 1 replica, then fail-over | 658            | 0                   | 0               |
