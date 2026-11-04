"""Regression tests promoted from the read-only review (shared_endpoint_review in scout_review.md): prune needs confirmed closure, the alert cap budgets attempts, cap exhaustion still cleans stale pending flags."""
import argparse
from datetime import date
import json

import pytest

import job_scout as js


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 3)
    monkeypatch.setattr(js, "date", FixedDate)


def job(id_):
    return dict(id=str(id_), title=f"Treasury Analyst {id_}", location="New York", url=f"https://example.com/{id_}", posted="2026-10-01")


def run_scout(tmp_path, monkeypatch, jobs, queue, state, *, notify_ok=True, cap=30):
    (tmp_path / "config.json").write_text(json.dumps(dict(companies=[dict(name="X", ats="fake")],
                                                         filters={}, keywords=[], notify={})), encoding="utf-8")
    (tmp_path / "queue.json").write_text(json.dumps(queue), encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setattr(js, "fetch_all", lambda companies: ({"X": (jobs, True, None)}, {}))
    monkeypatch.setattr(js, "describe", lambda company, job: ("", "", ""))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda queue, config: None)
    monkeypatch.setattr(js, "ALERT_CAP_PER_RUN", cap)
    deliveries = []
    monkeypatch.setattr(js, "notify", lambda config, entry, dry: deliveries.append(entry) or notify_ok)
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))
    return deliveries, json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))


def test_first_complete_miss_does_not_prune_a_recently_seen_old_posting(tmp_path, monkeypatch):
    old = dict(id="X:old", company="X", role="Treasury Analyst old", location="New York", state="new",
               posted="2026-01-01", last_seen_live="2026-10-02", first_seen="2026-01-01",
               miss_count=0, closed_on=None, repost_count=0)
    _, queue = run_scout(tmp_path, monkeypatch, [job("other")], [old], {"X": ["old", "other"]})
    surviving = [e for e in queue if e["id"] == "X:old"]
    assert surviving, "A first miss must not delete an old posting last seen yesterday"
    assert surviving[0]["miss_count"] == 1 and surviving[0]["closed_on"] is None


def test_failed_individual_sends_respect_attempt_budget(tmp_path, monkeypatch):
    old = [job(i) for i in range(20)]
    deliveries, _ = run_scout(tmp_path, monkeypatch, old + [job(i) for i in range(100, 103)], [],
                              {"X": [str(i) for i in range(20)]}, notify_ok=False, cap=2)
    individual = [e for e in deliveries if e["company"] != "Summary"]
    assert len(individual) <= 2, "Failed HTTP sends still consume the per-run individual attempt budget"


@pytest.mark.parametrize("inactive", [dict(closed_on="2026-10-02", state="new"),
                                      dict(closed_on=None, state="applied"),
                                      dict(closed_on=None, state="new", alert_attempts=js.ALERT_MAX_ATTEMPTS)])
def test_exhausted_cap_does_not_skip_pending_cleanup(monkeypatch, inactive):
    active = dict(id="X:active", state="new", closed_on=None, alert_pending=True, alert_attempts=1)
    canceled = dict(id="X:canceled", state="new", closed_on=None, alert_pending=True, alert_attempts=1)
    canceled.update(inactive)
    sent = []
    monkeypatch.setattr(js, "notify", lambda *args: sent.append(args) or True)
    js._retry_pending_alerts([active, canceled], {}, {"alerted": js.ALERT_CAP_PER_RUN}, False)
    assert not sent and active["alert_pending"]
    assert not canceled["alert_pending"], "Cleanup must visit all rows even when sending is capped"


def test_filter_change_triggers_recall_and_records_signature(tmp_path, monkeypatch):
    import argparse
    import json
    import job_scout as js
    old = dict(id="1", title="Treasury Analyst", location="New York, NY", url="u1")
    (tmp_path / "config.json").write_text(json.dumps(dict(notify={}, filters=dict(title_include=["treasury"]), keywords=[],
                                                         companies=[dict(name="X", ats="fake")])))
    (tmp_path / "state.json").write_text(json.dumps({"X": ["1"]}))  # seen earlier, never queued
    (tmp_path / "filters_sig.json").write_text(json.dumps(dict(sig="old")))
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setitem(js.FETCH, "fake", lambda c: ([old], True))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda q, c: None)
    monkeypatch.setattr(js, "describe", lambda c, j: ("", "", ""))
    monkeypatch.setattr(js, "notify", lambda *a: True)
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert [e["id"] for e in queue] == ["X:1"] and queue[0]["origin"] == "recall"
    assert json.loads((tmp_path / "filters_sig.json").read_text())["sig"] == js._filters_signature(dict(title_include=["treasury"]))
    # same filters again: no recall, nothing new
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))
    assert len(json.loads((tmp_path / "queue.json").read_text())) == 1
