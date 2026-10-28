// The next boundary: 20 API processes, 100 callers each, one cold key.
//
//	go run ./cmd/fleet
//
// Each process coalesces its own callers with coalesce.Group, so the database sees one
// read per process: 20, not 2,000 and not 1. A second key refreshes at the same time and
// does not wait behind the first.
package main

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ZeeshanAdilButt/distributed-systems-challenges/challenges/04-cache-stampede/go/coalesce"
)

func main() {
	const processes, callers = 20, 100
	var reads atomic.Int64
	read := func(ctx context.Context) (string, error) {
		reads.Add(1)
		select {
		case <-time.After(200 * time.Millisecond): // Slow database.
			return `{"comments":[{"id":"c901"}]}`, nil
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}

	var wg sync.WaitGroup
	var shared atomic.Int64
	start := make(chan struct{})
	for p := 0; p < processes; p++ {
		group := &coalesce.Group[string]{} // one map per process
		for i := 0; i < callers; i++ {
			wg.Add(1)
			go func(key string) {
				defer wg.Done()
				<-start
				ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
				defer cancel()
				if _, s, err := group.Do(ctx, key, read); err == nil && s {
					shared.Add(1)
				}
			}([]string{"p123", "p456"}[i%2])
		}
	}
	began := time.Now()
	close(start)
	wg.Wait()
	fmt.Printf("processes: %d, requests: %d, keys: 2, database reads: %d, shared results: %d, took: %dms\n",
		processes, processes*callers, reads.Load(), shared.Load(), time.Since(began).Milliseconds())
}
