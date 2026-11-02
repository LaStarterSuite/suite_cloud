package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWriteFileReplacesTheFile(t *testing.T) {
	dir := t.TempDir()
	for _, value := range []int{1, 2} {
		if err := WriteFile(dir, "counts.json", map[string]int{"a": value}); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(filepath.Join(dir, "counts.json"))
	if err != nil || string(raw) != "{\n  \"a\": 2\n}" {
		t.Fatalf("%q %v", raw, err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatalf("leftovers: %v", entries)
	}
}
