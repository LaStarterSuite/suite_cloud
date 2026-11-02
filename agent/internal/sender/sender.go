// Package sender calls Suite Cloud's agent methods under the per-minute request limit the site
// enforces.
package sender

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

const methodPrefix = "/api/v2/method/suite_cloud.api.telemetry."

const callAnswerLimit = 8 << 20 // a page of limits

type Sender struct {
	client    *http.Client
	base      string
	secret    string
	userAgent string
	limiter   *Limiter
	now       func() time.Time
}

func New(cfg config.SuiteCloud, version string) *Sender {
	return &Sender{
		client:    &http.Client{Timeout: time.Duration(cfg.RequestTimeoutSeconds) * time.Second},
		base:      strings.TrimRight(cfg.URL, "/") + methodPrefix,
		secret:    cfg.Secret,
		userAgent: "suite-cloud-agent/" + version,
		limiter:   NewLimiter(cfg.RequestsPerMinute),
		now:       time.Now,
	}
}

// Call posts one request to a method, such as "quota.limits", under the shared rate limit and
// decodes the answer's data into out. It makes one attempt: callers retry on their next tick.
func (s *Sender) Call(ctx context.Context, method string, payload, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	if err := s.limiter.Wait(ctx); err != nil {
		return err
	}
	status, answer, err := s.post(ctx, method, body, callAnswerLimit)
	switch {
	case err != nil:
		return err
	case status == http.StatusTooManyRequests:
		s.limiter.Exhaust(nextMinute(s.now()))
		return fmt.Errorf("%s: rate limited", method)
	case status != http.StatusOK:
		return fmt.Errorf("%s: status %d: %s", method, status, errorMessage(answer))
	}
	envelope := struct {
		Data any `json:"data"`
	}{out}
	if err := json.Unmarshal([]byte(answer), &envelope); err != nil {
		return fmt.Errorf("%s: answer not understood: %w", method, err)
	}
	return nil
}

// post makes one request. It outlives a cancelled context so a request on the wire completes and
// is committed; the client timeout bounds it.
func (s *Sender) post(ctx context.Context, method string, body []byte, answerLimit int64) (status int, message string, err error) {
	request, err := http.NewRequestWithContext(context.WithoutCancel(ctx), http.MethodPost, s.base+method, bytes.NewReader(body))
	if err != nil {
		return 0, "", err
	}
	request.Header.Set("Authorization", "Bearer "+s.secret)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", s.userAgent)

	response, err := s.client.Do(request)
	if err != nil {
		return 0, "", err
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, answerLimit))
	return response.StatusCode, string(raw), nil
}

// errorMessage pulls the message out of Frappe's {"errors": [{"type", "message"}]} body.
func errorMessage(raw string) string {
	var body struct {
		Errors []struct {
			Type    string `json:"type"`
			Message string `json:"message"`
		} `json:"errors"`
	}
	if json.Unmarshal([]byte(raw), &body) == nil && len(body.Errors) > 0 {
		return fmt.Sprintf("%s: %s", body.Errors[0].Type, body.Errors[0].Message)
	}
	return truncate(raw)
}

func truncate(text string) string {
	if len(text) > 500 {
		return text[:500] + "..."
	}
	return text
}
