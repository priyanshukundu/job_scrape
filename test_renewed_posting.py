import job_scout as js
from datetime import date, timedelta

C = {"name": "X", "ats": "workday", "site": "S", "host": "h", "tenant": "t"}


def _entry(posted):
    return {"id": "X:/job/a_R1", "company": "X", "posted": posted, "posted_source": "exact", "site": "S"}


def _days(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_renewed_requisition_moves_posted_forward(monkeypatch):
    e = _entry(_days(40))
    js._refresh_candidates.clear()
    js._note_possible_refresh(e, C, _days(0))
    monkeypatch.setattr(js, "_workday_refresh_posted", lambda c, j, s: (_days(0), "exact"))
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    assert js._apply_refreshed_dates() == 1
    assert e["posted"] == _days(0) and e["repost_count"] == 1 and e["reposted_on"] == _days(0)
    assert e["posted_before_refresh"] == _days(40)


def test_one_day_drift_is_ignored():
    e = _entry(_days(5))
    js._refresh_candidates.clear()
    js._note_possible_refresh(e, C, _days(4))
    assert js._refresh_candidates == []


def test_detail_not_confirming_leaves_date(monkeypatch):
    e = _entry(_days(40))
    js._refresh_candidates.clear()
    js._note_possible_refresh(e, C, _days(0))
    monkeypatch.setattr(js, "_workday_refresh_posted", lambda c, j, s: (_days(40), "exact"))
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    assert js._apply_refreshed_dates() == 0 and e["posted"] == _days(40)
