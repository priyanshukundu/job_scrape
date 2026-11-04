"""Crash-safe JSON writes: write to a temp file then os.replace() over the
target, so a crash mid-write never leaves a truncated/corrupt file."""
import json
import os
import tempfile


def write(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
