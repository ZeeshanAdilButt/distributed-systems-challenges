# Part 7 · Stale routers and epochs: results

## A. Vnode 60 moves from DB B to DB D (epoch 7 to 8); API 1 still has the old map, then writes 200 comments

|                                            | DB B checks the epoch (fenced) | DB B trusts the caller (no fence) |
|--------------------------------------------|--------------------------------|-----------------------------------|
| writes sent by the stale API 1             | 200                            | 200                               |
| refused with NOT_OWNER                     | 1                              | 0                                 |
| metadata reads the stale API paid for      | 1                              | 0                                 |
| succeeded on the retry, on DB D            | 1                              | 0                                 |
| readable afterwards from the current owner | 200 of 200                     | 0 of 200                          |
| rows stranded on the old owner             | 0                              | 200                               |

What happened, in order (fenced run):

1. API 1 caches the map: vnode 60 is on DB B, epoch 7
1. copy vnode 60: 100 rows B -> D
1. metadata flips vnode 60 to D, epoch 8
1. API 1 still believes DB B, epoch 7, and does not know
1. API 1 wrote 200 comments: 1 refused by DB B

## B. 100 writes whose reply is lost after the commit, so the API retries

| retry carries           | writes asked | rows stored | duplicates |
|-------------------------|--------------|-------------|------------|
| the same request id     | 100          | 100         | 0          |
| nothing: it starts over | 100          | 200         | 100        |
