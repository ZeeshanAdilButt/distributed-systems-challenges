// Package coalesce shares one call per key among concurrent callers.
//
//	cache key -> the refresh currently in progress
//
// The first caller for a key runs the function. Callers that arrive while it runs wait
// for its result, each only as long as its own context allows, and do not start another
// database query. Keys are independent: refreshing one post never blocks another.
//
// It coordinates one process. Twenty API processes that miss together still make up to
// twenty calls. That is accepted here: a distributed lock to save nineteen reads would
// add a dependency that can fail on exactly the bad day.
package coalesce

import (
	"context"
	"sync"
)

type call[V any] struct {
	done  chan struct{}
	value V
	err   error
}

// Group holds the refreshes in progress, one per key.
type Group[V any] struct {
	mu    sync.Mutex
	calls map[string]*call[V]
}

// Do runs fn for key, or waits for the run already in progress.
//
// shared reports whether the result came from another caller's run. A waiter whose
// context ends first gets ctx.Err() and leaves; the run carries on for the others. The
// leader runs fn with its own context, so give fn a query deadline.
func (g *Group[V]) Do(ctx context.Context, key string, fn func(context.Context) (V, error)) (v V, shared bool, err error) {
	g.mu.Lock()
	if g.calls == nil {
		g.calls = make(map[string]*call[V])
	}
	if c, ok := g.calls[key]; ok {
		g.mu.Unlock()
		select {
		case <-c.done:
			return c.value, true, c.err
		case <-ctx.Done():
			var zero V
			return zero, true, ctx.Err()
		}
	}
	c := &call[V]{done: make(chan struct{})}
	g.calls[key] = c
	g.mu.Unlock()

	c.value, c.err = fn(ctx)

	g.mu.Lock()
	delete(g.calls, key)
	g.mu.Unlock()
	close(c.done)
	return c.value, false, c.err
}
