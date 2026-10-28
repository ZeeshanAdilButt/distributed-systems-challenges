package coalesce

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func slowRead(reads *atomic.Int64, d time.Duration) func(context.Context) (string, error) {
	return func(ctx context.Context) (string, error) {
		reads.Add(1)
		select {
		case <-time.After(d):
			return `{"comments":[{"id":"c901"}]}`, nil
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

func TestHundredCallersOneRead(t *testing.T) {
	var g Group[string]
	var reads atomic.Int64
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			v, _, err := g.Do(context.Background(), "p123", slowRead(&reads, 200*time.Millisecond))
			if err != nil || v == "" {
				t.Errorf("got %q, %v", v, err)
			}
		}()
	}
	close(start)
	wg.Wait()
	if n := reads.Load(); n != 1 {
		t.Fatalf("database reads = %d, want 1", n)
	}
}

func TestOneKeyDoesNotBlockAnother(t *testing.T) {
	var g Group[string]
	var reads atomic.Int64
	go g.Do(context.Background(), "p123", slowRead(&reads, 500*time.Millisecond))
	time.Sleep(20 * time.Millisecond)
	began := time.Now()
	if _, _, err := g.Do(context.Background(), "p456", slowRead(&reads, 10*time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	if waited := time.Since(began); waited > 200*time.Millisecond {
		t.Fatalf("p456 waited %v behind p123", waited)
	}
}

func TestWaitersLeaveAtTheirOwnDeadline(t *testing.T) {
	var g Group[string]
	var reads atomic.Int64
	go g.Do(context.Background(), "p123", slowRead(&reads, time.Second))
	time.Sleep(20 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	began := time.Now()
	_, shared, err := g.Do(ctx, "p123", slowRead(&reads, time.Second))
	if !errors.Is(err, context.DeadlineExceeded) || !shared {
		t.Fatalf("err = %v, shared = %v", err, shared)
	}
	if waited := time.Since(began); waited > 300*time.Millisecond {
		t.Fatalf("waited %v, want about 100ms", waited)
	}
	if n := reads.Load(); n != 1 {
		t.Fatalf("database reads = %d, want 1", n)
	}
}

func TestTwentyProcessesTwentyReads(t *testing.T) {
	var reads atomic.Int64
	var wg sync.WaitGroup
	start := make(chan struct{})
	for p := 0; p < 20; p++ {
		g := &Group[string]{} // each API process has its own map
		for i := 0; i < 100; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				g.Do(context.Background(), "p123", slowRead(&reads, 100*time.Millisecond))
			}()
		}
	}
	close(start)
	wg.Wait()
	if n := reads.Load(); n != 20 {
		t.Fatalf("database reads = %d, want 20", n)
	}
}
