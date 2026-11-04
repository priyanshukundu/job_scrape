"""Offline tests: terms page past max_per_term to the reported total, and a posting the list diff missed
is only closed when its own lookup says it is gone."""
import urllib.error
from datetime import date
from unittest import mock

import job_scout as js


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def _wd_company(**extra):
    return dict(name="Acme", ats="workday", host="acme.wd1.myworkdayjobs.com", tenant="acme", site="Careers",
                search=["risk"], max_per_term=40, **extra)


def _wd_pages(total, ceiling_seen):
    def fake(url, data=None, **kw):
        off = data["offset"]
        ceiling_seen.append(off)
        n = max(0, min(20, total - off))
        body = {"jobPostings": [{"externalPath": f"/job/NYC/T{off + i}_R{off + i}", "title": f"T{off + i}",
                                 "locationsText": "NYC", "postedOn": "Posted Today"} for i in range(n)]}
        if off == 0:
            body["total"] = total
        return body
    return fake


def test_workday_pages_past_cap_to_total_and_completes():
    seen = []
    with mock.patch.object(js, "http", _wd_pages(130, seen)):
        jobs, complete = js.workday(_wd_company())
    assert len(jobs) == 130 and complete is True
    assert max(seen) == 120


def test_workday_ceiling_limits_paging_and_marks_incomplete():
    seen = []
    with mock.patch.object(js, "http", _wd_pages(500, seen)):
        jobs, complete = js.workday(_wd_company(term_ceiling=100))
    assert len(jobs) == 100 and complete is False


def test_workday_small_term_unchanged():
    seen = []
    with mock.patch.object(js, "http", _wd_pages(15, seen)):
        jobs, complete = js.workday(_wd_company())
    assert len(jobs) == 15 and complete is True and seen == [0]


def _queue_entry(ats_path="/job/NYC/Risk-Analyst_R-0000123", **kw):
    row = dict(id=f"Acme:{ats_path}", company="Acme", role="Risk Analyst", location="NYC", state="new",
               first_seen="2026-09-01", last_seen_live="2026-10-01", miss_count=1, closed_on=None,
               closed_reason=None, repost_count=0, site="Careers")
    row.update(kw)
    return row


def _run_liveness(entry, c, still_listed_http):
    counts = dict(closed=0, reopened=0, reposts=0)
    other = {"id": "/job/NYC/Other_R-1", "title": "Other", "location": "NYC", "url": "u", "site": "Careers"}
    with mock.patch.object(js, "http", still_listed_http):
        js._update_liveness("Acme", [other], True, [entry], counts, c)
    return counts


def test_workday_close_is_blocked_when_req_id_still_listed():
    e = _queue_entry()
    hit = lambda url, data=None, **kw: {"jobPostings": [{"externalPath": "/job/NYC/Risk-Analyst_R-0000123"}]}
    counts = _run_liveness(e, _wd_company(), hit)
    assert e["closed_on"] is None and counts["closed"] == 0 and e["miss_count"] == 0


def test_workday_close_proceeds_when_req_id_gone():
    e = _queue_entry()
    miss = lambda url, data=None, **kw: {"jobPostings": []}
    counts = _run_liveness(e, _wd_company(), miss)
    assert e["closed_reason"] == "removed" and counts["closed"] == 1


def test_lookup_failure_keeps_posting_open():
    e = _queue_entry()

    def boom(url, data=None, **kw):
        raise OSError("network down")
    counts = _run_liveness(e, _wd_company(), boom)
    assert e["closed_on"] is None and counts["closed"] == 0


def test_oracle_and_eightfold_and_smartrecruiters_lookups():
    ora = dict(name="Acme", ats="oracle", host="h.example", site="CX_1")
    e = _queue_entry("/1234")
    with mock.patch.object(js, "http", lambda url, **kw: {"items": [{"Id": 1}]}):
        assert js._still_listed(ora, e) is True
    with mock.patch.object(js, "http", lambda url, **kw: {"items": []}):
        assert js._still_listed(ora, e) is False
    ef = dict(name="Acme", ats="eightfold", host="h.example", domain="acme.com")
    with mock.patch.object(js, "http", mock.Mock(side_effect=_http_error(404))):
        assert js._still_listed(ef, e) is False
    with mock.patch.object(js, "http", mock.Mock(side_effect=_http_error(500))):
        assert js._still_listed(ef, e) is True
    sr = dict(name="Acme", ats="smartrecruiters", slug="acme")
    with mock.patch.object(js, "http", mock.Mock(side_effect=_http_error(404))):
        assert js._still_listed(sr, e) is False
    with mock.patch.object(js, "http", lambda url, **kw: {"id": "1"}):
        assert js._still_listed(sr, e) is True
