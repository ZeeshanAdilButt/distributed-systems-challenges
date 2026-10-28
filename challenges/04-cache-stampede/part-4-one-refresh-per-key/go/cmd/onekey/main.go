// 100 callers, one database read: one key, one process, no expiry and no errors.
//
//	go run ./cmd/onekey
//
// One mutex guards the whole cache, so while the first caller reads the database every
// other caller waits, and then finds the value. That is the idea of coalescing in its
// smallest form. Two things make it unfit for production: refreshing one post blocks every
// other post, and nobody bounds how long a caller waits. See ../../coalesce for both fixed.
package main

import (
	"fmt"
	"sync"
	"time"
)

func main() {
	var mu sync.Mutex
	var wg sync.WaitGroup
	value := ""
	dbReads := 0

	get := func() string {
		mu.Lock()
		defer mu.Unlock()

		if value == "" {
			dbReads++
			time.Sleep(200 * time.Millisecond) // Slow database.
			value = `{"comments":[{"id":"c901"}]}`
		}
		return value
	}

	start := make(chan struct{})
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_ = get()
		}()
	}
	close(start)
	wg.Wait()
	fmt.Println("requests: 100, database reads:", dbReads)
}
