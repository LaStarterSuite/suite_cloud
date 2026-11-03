package quota

import (
	"cmp"
	"context"
	"errors"
	"log/slog"
	"time"
)

const (
	fullPullInterval = time.Hour // also catches a save committed after a newer one was pulled
	finalPushTimeout = 5 * time.Second
	// A hook call can count into yesterday a moment before midnight and touch its account after
	// the day's first push took the touched accounts, so yesterday is sent for a while longer.
	yesterdayGrace = 10 * time.Minute
	limitsMethod   = "quota.limits"
	usageMethod    = "quota.usage"
	usageBatchSize = 5000 // Suite Cloud's default records per request
)

// Limits copies the cluster's limits from Suite Cloud into the store: what changed since the last
// pull, and everything on start and every hour.
type Limits struct {
	Store    Store
	Caller   Caller
	HostIP   string
	Interval time.Duration
	Logger   *slog.Logger
	Now      func() time.Time
	since    string
	lastFull time.Time
	missing  map[string]bool // accounts the last full pull did not return
}

type limitsPage struct {
	Limits []struct {
		Account string `json:"account"`
		Limit
	} `json:"limits"`
	NextVersion string `json:"next_version"`
	HasMore     bool   `json:"has_more"`
}

func (l *Limits) Run(ctx context.Context) error {
	ticker := time.NewTicker(l.Interval)
	defer ticker.Stop()
	for {
		full := l.lastFull.IsZero() || l.Now().Sub(l.lastFull) >= fullPullInterval
		if err := l.Pull(ctx, full); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			l.Logger.Warn("limits not pulled; the stored ones stay in force", "full", full, "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// Pull reads every page. A full pull also clears the limits of accounts that this and the previous
// full pull did not return, which is how a deleted account loses them. One miss is not enough: a
// save younger than the settle window is left out of a pull, and the account would send without
// a limit until the next one.
func (l *Limits) Pull(ctx context.Context, full bool) error {
	since := l.since
	if full {
		since = ""
	}
	seen := map[string]bool{}
	for {
		var page limitsPage
		if err := l.Caller.Call(ctx, limitsMethod, map[string]any{"host_ip": l.HostIP, "since_version": since}, &page); err != nil {
			return err
		}
		limits := make(map[string]Limit, len(page.Limits))
		for _, entry := range page.Limits {
			account := Account(entry.Account)
			limits[account] = entry.Limit
			seen[account] = true
		}
		if err := l.Store.SetLimits(ctx, limits); err != nil {
			return err
		}
		if page.NextVersion != "" {
			since = page.NextVersion
		}
		if !page.HasMore {
			break
		}
	}
	if full {
		if err := l.clearMissing(ctx, seen); err != nil {
			return err
		}
		l.lastFull = l.Now()
	}
	l.since = since
	return nil
}

func (l *Limits) clearMissing(ctx context.Context, seen map[string]bool) error {
	stored, err := l.Store.Accounts(ctx)
	if err != nil {
		return err
	}
	missing := map[string]bool{}
	var gone []string
	for _, account := range stored {
		if seen[account] {
			continue
		}
		missing[account] = true
		if l.missing[account] {
			gone = append(gone, account)
		}
	}
	if err := l.Store.ClearLimits(ctx, gone); err != nil {
		return err
	}
	l.missing = missing
	return nil
}

// Usage pushes the day and month totals of the accounts counted since the last push.
type Usage struct {
	Store     Store
	Caller    Caller
	HostIP    string
	Touched   *Touched
	Interval  time.Duration
	BatchSize int // records per request; zero means usageBatchSize
	Logger    *slog.Logger
	Now       func() time.Time
	pushed    Periods           // the periods of the last push that went through; zero until one does
	counted   bool              // the accounts counted before the start were touched
	owed      map[string]string // account -> yesterday's day, whose record a failed request held
}

type usageRecord struct {
	Account string `json:"account"`
	Period  string `json:"period"`
	Sent    int64  `json:"sent"`
	Refused int64  `json:"refused"`
}

// Run pushes on every tick, and once more on the way out. Until the store lists the accounts it
// counted before the start, every tick asks again.
func (u *Usage) Run(ctx context.Context) error {
	u.TouchCounted(ctx)
	ticker := time.NewTicker(u.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			final, cancel := context.WithTimeout(context.WithoutCancel(ctx), finalPushTimeout)
			defer cancel()
			if err := u.Push(final); err != nil {
				u.Logger.Warn("usage not pushed on shutdown", "error", err)
			}
			return ctx.Err()
		case <-ticker.C:
			if !u.counted {
				u.TouchCounted(ctx)
			}
			if err := u.Push(ctx); err != nil && !errors.Is(err, context.Canceled) {
				u.Logger.Warn("usage not pushed; it is sent with the next push", "error", err)
			}
		}
	}
}

// TouchCounted marks every account with a count today or yesterday, as on a start: what was
// counted before a crash, or after the last push before one, is then pushed. Suite Cloud keeps
// the higher count, so sending one again is harmless.
func (u *Usage) TouchCounted(ctx context.Context) {
	now := u.Now()
	accounts, err := u.Store.CountedAccounts(ctx, PeriodsAt(now).Day, PeriodsAt(now.Add(-24*time.Hour)).Day)
	if err != nil {
		u.Logger.Warn("counted accounts not read; asking again at the next push", "error", err)
		return
	}
	u.Touched.Add(accounts...)
	u.counted = true
}

// Push sends the touched accounts' records in requests of at most BatchSize records. Accounts
// with a record in a request that was not taken stay touched.
// Until a push of the current day has gone through (after midnight UTC, after a start, after
// failures), and in the first minutes of every UTC day, each push also sends yesterday's day and
// month: what was counted after the last push of yesterday would otherwise never be reported. An
// account whose yesterday record a failed request held gets it again until it is taken. Suite
// Cloud takes nothing older than yesterday.
func (u *Usage) Push(ctx context.Context) error {
	accounts := u.Touched.Take()
	if len(accounts) == 0 {
		return nil
	}
	now := u.Now()
	periods := pushPeriods{current: PeriodsAt(now), yesterday: PeriodsAt(now.Add(-24 * time.Hour))}
	everyone := u.pushed != periods.current || now.Sub(now.UTC().Truncate(24*time.Hour)) < yesterdayGrace
	records, err := u.getRecords(ctx, accounts, periods, everyone)
	if err != nil {
		u.Touched.Add(accounts...)
		return err
	}
	u.dropStaleOwed(periods.yesterday.Day)
	if err := u.send(ctx, records, periods); err != nil {
		return err
	}
	u.pushed = periods.current
	return nil
}

// pushPeriods are the periods of one push: the current day and month, and yesterday's.
type pushPeriods struct{ current, yesterday Periods }

// isLate reports whether a record is one of yesterday's.
func (p pushPeriods) isLate(record usageRecord) bool {
	return record.Period == p.yesterday.Day || record.Period == p.yesterday.Month && p.yesterday.Month != p.current.Month
}

// lateRecords are an account's records of yesterday. Before midnight, only what was counted: an
// account first counted after it has nothing.
func (p pushPeriods) lateRecords(account string, before Count) []usageRecord {
	var records []usageRecord
	if before.Day > 0 || before.RefusedDay > 0 {
		records = append(records, usageRecord{account, p.yesterday.Day, before.Day, before.RefusedDay})
	}
	if p.yesterday.Month != p.current.Month && (before.Month > 0 || before.RefusedMonth > 0) {
		records = append(records, usageRecord{account, p.yesterday.Month, before.Month, before.RefusedMonth})
	}
	return records
}

// getRecords reads the accounts' current records, and yesterday's of everyone or of the accounts
// that are owed them.
func (u *Usage) getRecords(ctx context.Context, accounts []string, periods pushPeriods, everyone bool) ([]usageRecord, error) {
	counts, err := u.Store.Counts(ctx, accounts, periods.current)
	if err != nil {
		return nil, err
	}
	lateCounts, err := u.getLateCounts(ctx, accounts, periods.yesterday, everyone)
	if err != nil {
		return nil, err
	}
	var records []usageRecord
	for _, account := range accounts {
		count := counts[account]
		records = append(records,
			usageRecord{account, periods.current.Day, count.Day, count.RefusedDay},
			usageRecord{account, periods.current.Month, count.Month, count.RefusedMonth},
		)
		if before, ok := lateCounts[account]; ok {
			records = append(records, periods.lateRecords(account, before)...)
		}
	}
	return records, nil
}

func (u *Usage) getLateCounts(ctx context.Context, accounts []string, yesterday Periods, everyone bool) (map[string]Count, error) {
	var late []string
	for _, account := range accounts {
		if everyone || u.owed[account] == yesterday.Day {
			late = append(late, account)
		}
	}
	if len(late) == 0 {
		return nil, nil
	}
	return u.Store.Counts(ctx, late, yesterday)
}

// dropStaleOwed forgets what is owed of days before yesterday: Suite Cloud would not take it.
func (u *Usage) dropStaleOwed(yesterday string) {
	owed := map[string]string{}
	for account, day := range u.owed {
		if day == yesterday {
			owed[account] = day
		}
	}
	u.owed = owed
}

// send posts the records in requests of at most BatchSize records. When one fails, the accounts
// of the records not taken stay touched, and their yesterday records owed.
func (u *Usage) send(ctx context.Context, records []usageRecord, periods pushPeriods) error {
	size := cmp.Or(u.BatchSize, usageBatchSize)
	for start := 0; start < len(records); start += size {
		batch := records[start:min(start+size, len(records))]
		var answer struct{}
		if err := u.Caller.Call(ctx, usageMethod, map[string]any{"host_ip": u.HostIP, "records": batch}, &answer); err != nil {
			u.keepForNextPush(records[start:], periods)
			return err
		}
		for _, record := range batch {
			if periods.isLate(record) {
				delete(u.owed, record.Account)
			}
		}
	}
	return nil
}

func (u *Usage) keepForNextPush(records []usageRecord, periods pushPeriods) {
	for _, record := range records {
		u.Touched.Add(record.Account)
		if periods.isLate(record) {
			u.owed[record.Account] = periods.yesterday.Day
		}
	}
}
