"""Read-only review coverage for missing shards, partial boards and dry runs."""
import argparse
from datetime import date
import json

import pytest

import job_scout as js


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    company = {"name": "X", "ats": "fake"}
    queue = [{"id": "X:old", "company": "X", "state": "new", "posted": "2020-01-01",
              "role": "Treasury Analyst", "location": "NY", "miss_count": 0,
              "last_seen_live": "2020-01-02", "closed_on": None}]
    config = dict(companies=[company], filters={}, keywords=[], notify={})
    for filename, content in (("config.json", config), ("queue.json", queue), ("state.json", {"X": ["old"]})):
        (tmp_path / filename).write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setattr(js, "FETCH_TIMES_PATH", tmp_path / "fetch_times.json")
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda *args: None)
    monkeypatch.setattr(js, "describe", lambda *args: ("", "", ""))
    shards = tmp_path / "shards"
    shards.mkdir()
    return tmp_path, shards


@pytest.mark.parametrize("outcome", ["missing", "error", "incomplete", "empty"])
def test_unverified_shard_outcomes_keep_old_queue_rows(inputs, outcome):
    home, shards = inputs
    job = dict(id="other", title="Different", location="NY", url="https://example.com/other", posted=date.today().isoformat())
    if outcome != "missing":
        row = dict(jobs=[job], complete=True, err=None)
        if outcome == "error":
            row.update(jobs=None, complete=None, err="HTTP 500")
        elif outcome == "incomplete":
            row["complete"] = False
        else:
            row["jobs"] = []
        (shards / "shard_0.json").write_text(json.dumps({"X": row}), encoding="utf-8")
    js.run(argparse.Namespace(from_fetched=str(shards), dry_run=False, check=False))
    queue = json.loads((home / "queue.json").read_text(encoding="utf-8"))
    old = next(e for e in queue if e["id"] == "X:old")
    assert old["miss_count"] == 0 and old["closed_on"] is None
    assert "old" in json.loads((home / "state.json").read_text(encoding="utf-8"))["X"]


def test_empty_first_shard_result_does_not_seed_state(inputs):
    home, shards = inputs
    (home / "queue.json").write_text("[]", encoding="utf-8")
    (home / "state.json").write_text("{}", encoding="utf-8")
    (shards / "shard_0.json").write_text(json.dumps({"X": dict(jobs=[], complete=True, err=None)}), encoding="utf-8")
    js.run(argparse.Namespace(from_fetched=str(shards), dry_run=False, check=False))
    assert "X" not in json.loads((home / "state.json").read_text(encoding="utf-8"))


def test_dry_run_from_shards_never_writes_or_delivers_network_alerts(inputs, monkeypatch, capsys):
    home, shards = inputs
    job = dict(id="fresh", title="Different", location="NY", url="https://example.com/fresh", posted=date.today().isoformat())
    (shards / "shard_0.json").write_text(json.dumps({"X": dict(jobs=[job], complete=True, err=None)}), encoding="utf-8")
    before = {p: p.read_bytes() for p in home.rglob("*.json")}

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run must not write JSON or send HTTP notifications")

    monkeypatch.setattr(js.atomic_json, "write", forbidden)
    monkeypatch.setattr(js, "http", forbidden)
    js.run(argparse.Namespace(from_fetched=str(shards), dry_run=True, check=False))
    assert {p: p.read_bytes() for p in home.rglob("*.json")} == before
    assert "[dry-run]" in capsys.readouterr().out
