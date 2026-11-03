package sender

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

// clock stands in for time: sleeping moves it forward at once and records the wait.
type clock struct {
	mu    sync.Mutex
	now   time.Time
	waits []time.Duration
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) Sleep(ctx context.Context, d time.Duration) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	c.waits = append(c.waits, d)
	return nil
}

type request struct {
	path, auth, userAgent string
	body                  []byte
}

func newServer(t *testing.T, statuses ...int) (*httptest.Server, *[]request) {
	t.Helper()
	var requests []request
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		requests = append(requests, request{r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("User-Agent"), body})
		n := len(requests)
		mu.Unlock()
		status := statuses[min(n, len(statuses))-1]
		w.WriteHeader(status)
		switch status {
		case http.StatusOK:
			var envelope struct {
				Records []any `json:"records"`
			}
			json.Unmarshal(body, &envelope)
			fmt.Fprintf(w, `{"data": {"accepted": %d}}`, len(envelope.Records))
		default:
			fmt.Fprintf(w, `{"errors": [{"type": "SomeError", "message": "status %d"}]}`, status)
		}
	}))
	t.Cleanup(server.Close)
	return server, &requests
}

func newSender(t *testing.T, url string) (*Sender, *clock) {
	t.Helper()
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 30, 0, time.UTC)}
	s := New(config.SuiteCloud{URL: url + "/", Secret: "s3cret", RequestsPerMinute: 300, RequestTimeoutSeconds: 5}, "test")
	s.now = c.Now
	s.limiter.now, s.limiter.sleep = c.Now, c.Sleep
	return s, c
}

func TestLimiterSharesOneMinuteWindow(t *testing.T) {
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 10, 0, time.UTC)}
	l := NewLimiter(3)
	l.now, l.sleep = c.Now, c.Sleep
	for i := 0; i < 3; i++ {
		if err := l.Wait(context.Background()); err != nil || len(c.waits) != 0 {
			t.Fatalf("blocked early: %v", c.waits)
		}
	}
	if err := l.Wait(context.Background()); err != nil || len(c.waits) != 1 || c.waits[0] != 50*time.Second {
		t.Fatalf("waits %v", c.waits)
	}
	l.Exhaust(c.Now().Add(63 * time.Second)) // a 429: closed until past the next boundary
	l.Wait(context.Background())
	if len(c.waits) != 2 || c.waits[1] != 63*time.Second {
		t.Fatalf("waits %v", c.waits)
	}
	if l.Wait(context.Background()); len(c.waits) != 2 {
		t.Fatalf("the new window did not open: %v", c.waits)
	}
}

// Goroutines that find the minute full each sleep, and the fake clock adds their sleeps up, so the
// number of waits varies; what must hold is that no minute grants more than its share.
func TestLimiterIsSafeAcrossGoroutines(t *testing.T) {
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)}
	l := NewLimiter(100)
	granted := map[time.Time]int{}
	l.now = func() time.Time {
		granted[l.window] = l.used // take holds l.mu here, so every earlier grant is counted
		return c.Now()
	}
	l.sleep = c.Sleep
	var wg sync.WaitGroup
	var failed atomic.Int32
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				if l.Wait(context.Background()) != nil {
					failed.Add(1)
				}
			}
		}()
	}
	wg.Wait()
	l.mu.Lock()
	granted[l.window] = l.used
	l.mu.Unlock()

	total := 0
	for window, n := range granted {
		if n > 100 {
			t.Fatalf("the minute from %s granted %d of 100", window.Format(time.TimeOnly), n)
		}
		total += n
	}
	if failed.Load() != 0 || total != 200 || len(c.waits) == 0 {
		t.Fatalf("%d failed, %d granted, %d waits", failed.Load(), total, len(c.waits))
	}
}

func TestErrorMessage(t *testing.T) {
	if got := errorMessage(`{"errors": [{"type": "InvalidInputError", "message": "records[12].timestamp must carry a UTC offset."}]}`); got != "InvalidInputError: records[12].timestamp must carry a UTC offset." {
		t.Fatal(got)
	}
	if got := errorMessage("<html>gateway"); got != "<html>gateway" {
		t.Fatal(got)
	}
}

func TestCallDecodesTheAnswerData(t *testing.T) {
	server, requests := newServer(t, 200)
	s, _ := newSender(t, server.URL)
	var answer struct {
		Accepted int `json:"accepted"`
	}
	payload := map[string]any{"batch_id": "b", "records": []int{1, 2}}
	if err := s.Call(context.Background(), "quota.usage", payload, &answer); err != nil {
		t.Fatal(err)
	}
	r := (*requests)[0]
	if r.path != "/api/v2/method/suite_cloud.api.telemetry.quota.usage" || r.auth != "Bearer s3cret" || r.userAgent != "suite-cloud-agent/test" || answer.Accepted != 2 {
		t.Fatalf("request %+v answer %+v", r, answer)
	}
}

func TestCallMakesOneAttempt(t *testing.T) {
	for _, status := range []int{400, 429, 500} {
		server, requests := newServer(t, status, 200)
		s, _ := newSender(t, server.URL)
		if err := s.Call(context.Background(), "quota.limits", map[string]any{}, &struct{}{}); err == nil {
			t.Fatalf("status %d accepted", status)
		}
		if len(*requests) != 1 {
			t.Fatalf("status %d: %d requests", status, len(*requests))
		}
	}
}

func TestRateLimitedCallHoldsTheNextUntilTheNextMinute(t *testing.T) {
	server, requests := newServer(t, 429, 200)
	s, c := newSender(t, server.URL)
	if err := s.Call(context.Background(), "quota.limits", map[string]any{}, &struct{}{}); err == nil {
		t.Fatal("429 accepted")
	}
	if err := s.Call(context.Background(), "quota.limits", map[string]any{}, &struct{}{}); err != nil {
		t.Fatal(err)
	}
	if len(*requests) != 2 || len(c.waits) != 1 || c.waits[0] < 31*time.Second || c.waits[0] > 36*time.Second {
		t.Fatalf("%d requests, waited %v, want until the next minute", len(*requests), c.waits)
	}
}
