package sender

import (
	"context"
	"math/rand/v2"
	"sync"
	"time"
)

// Limiter allows requests_per_minute requests per wall-clock minute, the fixed window Suite Cloud
// counts per host. The limit pull and the usage push share one Limiter because the server counts
// them together.
type Limiter struct {
	mu           sync.Mutex
	perMinute    int
	window       time.Time
	used         int
	blockedUntil time.Time
	now          func() time.Time
	sleep        func(context.Context, time.Duration) error
}

func NewLimiter(perMinute int) *Limiter {
	return &Limiter{perMinute: perMinute, now: time.Now, sleep: sleep}
}

// Wait blocks until a request may be made in the current minute.
func (l *Limiter) Wait(ctx context.Context) error {
	for {
		wait := l.take()
		if wait == 0 {
			return nil
		}
		if err := l.sleep(ctx, wait); err != nil {
			return err
		}
	}
}

// Exhaust marks the window as spent until the given time: the server answered 429.
func (l *Limiter) Exhaust(until time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.blockedUntil = until
	l.used = l.perMinute
}

func (l *Limiter) take() time.Duration {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	minute := now.Truncate(time.Minute)
	if minute.After(l.window) {
		l.window, l.used = minute, 0
	}
	switch {
	case now.Before(l.blockedUntil):
		return l.blockedUntil.Sub(now)
	case l.used >= l.perMinute:
		return minute.Add(time.Minute).Sub(now)
	}
	l.used++
	return 0
}

// nextMinute is when the server's window opens again, a little past the boundary so a skewed
// clock does not land the retry in the same window.
func nextMinute(now time.Time) time.Time {
	return now.Truncate(time.Minute).Add(time.Minute + time.Second + time.Duration(rand.IntN(4000))*time.Millisecond)
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
