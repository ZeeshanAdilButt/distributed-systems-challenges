# Part 1 · Why hash % n breaks: results

## Share of 100,000 posts that move when one database joins

| databases | hash % n | 12 fixed buckets | ring, 1 token each | ring, 64 vnodes each | key ranges | least possible |
|-----------|----------|------------------|--------------------|----------------------|------------|----------------|
| 3 -> 4    | 75.0%    | 25.0%            | 0.69%              | 28.3%                | 16.7%      | 25.0%          |
| 4 -> 5    | 80.0%    | 16.7%            | 97.3%              | 25.6%                | 12.5%      | 20.0%          |
| 5 -> 6    | 83.4%    | 16.6%            | 0.30%              | 19.1%                | 10.0%      | 16.7%          |
| 6 -> 7    | 85.7%    | 8.2%             | 0.67%              | 20.2%                | 8.3%       | 14.3%          |
| 7 -> 8    | 87.4%    | 8.2%             | 0.58%              | 13.1%                | 7.1%       | 12.5%          |
| 8 -> 9    | 88.9%    | 8.2%             | 0.81%              | 9.3%                 | 6.2%       | 11.1%          |

Did every moved post go to the new database (nothing reshuffled between old ones)?

| databases | hash % n | 12 fixed buckets | ring, 1 token each | ring, 64 vnodes each | key ranges |
|-----------|----------|------------------|--------------------|----------------------|------------|
| 3 -> 4    | no       | yes              | yes                | yes                  | yes        |
| 4 -> 5    | no       | yes              | yes                | yes                  | yes        |
| 5 -> 6    | no       | yes              | yes                | yes                  | yes        |
| 6 -> 7    | no       | yes              | yes                | yes                  | yes        |
| 7 -> 8    | no       | yes              | yes                | yes                  | yes        |
| 8 -> 9    | no       | yes              | yes                | yes                  | yes        |
