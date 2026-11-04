"""Offline pytest regression tests for closure, reposts and ghost classification."""
from datetime import date, timedelta

import pytest

import job_scout as js


TODAY = date(2026, 10, 3)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(js, "date", FixedDate)


@pytest.fixture
def counts():
    return dict(closed=0, reopened=0, reposts=0)


def days_ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def entry(id_, company="Acme", role="Risk Analyst", location="NYC", **fields):
    row = dict(id=id_, company=company, role=role, location=location, state="new",
               first_seen="2026-01-01", last_seen_live="2026-01-01", miss_count=0,
               closed_on=None, closed_reason=None, repost_count=0, repost_of=None, reposted_on=None)
    row.update(fields)
    return row


def find_repost(queue, live_ids=None, claimed=None):
    return js._find_repost_source(queue, "Acme", js.role_fingerprint("Risk Analyst", "NYC"),
                                 live_ids=live_ids or set(), eid="Acme:new", claimed=claimed or set())


def test_complete_listing_closes_only_after_required_misses(counts):
    queue = [entry("Acme:1"), entry("Acme:2")]
    for misses in range(1, js.CLOSE_AFTER_MISSES):
        js._update_liveness("Acme", [{"id": "2"}], True, queue, counts)
        assert queue[0]["closed_on"] is None and queue[0]["miss_count"] == misses
    js._update_liveness("Acme", [{"id": "2"}], True, queue, counts)
    assert queue[0]["closed_on"] == TODAY.isoformat()
    assert queue[0]["closed_reason"] == "removed" and counts["closed"] == 1
    assert queue[1]["closed_on"] is None and queue[1]["miss_count"] == 0


def test_truncated_listing_never_closes_or_increments_misses(counts):
    queue = [entry("Acme:1")]
    js._update_liveness("Acme", [{"id": "2"}], False, queue, counts)
    assert queue[0]["closed_on"] is None and queue[0]["miss_count"] == 0


def test_empty_listing_skips_liveness(counts):
    queue = [entry("Acme:1")]
    js._update_liveness("Acme", [], True, queue, counts)
    assert queue[0]["closed_on"] is None and queue[0]["miss_count"] == 0


def test_mass_vanish_requires_more_misses_but_eventually_closes(counts):
    queue = [entry(f"Acme:{i}") for i in range(5)]
    for _ in range(js.CLOSE_AFTER_MISSES):
        js._update_liveness("Acme", [{"id": "unrelated"}], True, queue, counts)
    assert all(row["closed_on"] is None for row in queue)
    for _ in range(js.CLOSE_AFTER_MISSES * 2):
        js._update_liveness("Acme", [{"id": "unrelated"}], True, queue, counts)
    assert all(row["closed_on"] == TODAY.isoformat() for row in queue)
    assert counts["closed"] == 5


def test_same_id_reappearance_reopens(counts):
    queue = [entry("Acme:1", closed_on="2026-01-01", closed_reason="removed", miss_count=2)]
    js._update_liveness("Acme", [{"id": "1"}], True, queue, counts)
    assert queue[0]["closed_on"] is None and queue[0]["closed_reason"] is None
    assert queue[0]["repost_count"] == 1 and queue[0]["miss_count"] == 0
    assert queue[0]["reposted_on"] == TODAY.isoformat() and counts["reopened"] == 1


def test_manual_close_stays_closed(counts):
    queue = [entry("Acme:1", closed_on="2026-01-01", closed_reason="manual")]
    js._update_liveness("Acme", [{"id": "1"}], True, queue, counts)
    assert queue[0]["closed_on"] == "2026-01-01" and counts["reopened"] == 0


def test_fingerprint_ignores_requisition_suffix():
    assert js.role_fingerprint("Senior Associate, Capital Markets & Risk (R249058-2)", "McLean, VA") == js.role_fingerprint(
        "Senior Associate, Capital Markets & Risk (R249058-3)", "McLean, VA")


def test_repost_only_matches_when_prior_id_is_gone():
    queue = [entry("Acme:old", first_seen=days_ago(5), last_seen_live=days_ago(5))]
    assert find_repost(queue, live_ids={"old"}) is None
    assert find_repost(queue)["id"] == "Acme:old"


def test_repost_window_uses_recent_closure_not_old_first_seen():
    queue = [entry("Acme:longlived", first_seen=days_ago(400), last_seen_live=days_ago(5), closed_on=days_ago(5))]
    assert find_repost(queue)["id"] == "Acme:longlived"


def test_stale_closure_does_not_match_recent_first_seen():
    queue = [entry("Acme:longgone", first_seen=days_ago(5), closed_on=days_ago(400))]
    assert find_repost(queue) is None


def test_two_new_postings_cannot_claim_same_prior():
    queue = [entry("Acme:shared", first_seen=days_ago(5), last_seen_live=days_ago(5))]
    first = find_repost(queue)
    assert first is not None
    assert find_repost(queue, claimed={first["id"]}) is None


@pytest.mark.parametrize("appearances,expected", [
    ([(200, 170), (150, 100), (60, None)], "Likely"),
    ([(200, 170), (150, None)], "Unclear"),
    ([(30, 20), (25, None), (10, None)], "Unclear"),
    ([(50, 45), (40, 35), (30, None)], "Unclear"),
])
def test_ghost_classification_requires_count_span_and_serial_postings(appearances, expected):
    queue = [entry(f"Acme:g{i}", role="Ghost Role", first_seen=days_ago(first),
                   closed_on=days_ago(closed) if closed is not None else None)
             for i, (first, closed) in enumerate(appearances)]
    js._classify_ghosts(queue)
    assert all(row["ghost_status"] == expected for row in queue)
    if expected == "Likely":
        assert all(row["ghost_evidence"] for row in queue)
