// Package state writes the files the agent keeps in its state directory.
package state

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// WriteFile writes value as JSON through a temp file and a rename, so a crash leaves the old file
// or the new one.
func WriteFile(dir, name string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temp, err := os.CreateTemp(dir, name+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.Write(raw); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(temp.Name(), filepath.Join(dir, name))
}
