"""Offline shape test for the csod fetcher (Cornerstone OnDemand career sites, e.g. FHLB Cincinnati)."""
from unittest import mock

import job_scout as js

HOME = b'<script>var c={"cloud":"https://us.api.csod.com/","token":"TOK.EN"}</script>'


def req(i, loc):
    return {"requisitionId": i, "displayJobTitle": f" Job {i} ", "postingEffectiveDate": "9/4/2026",
            "locations": loc, "externalDescription": "<p>Do &amp; things</p>"}


def test_csod_pages_token_and_rows():
    pages = {1: {"data": {"totalCount": 3, "requisitions": [req(1, [{"city": "Cincinnati", "state": "OH", "country": "US"}]), req(2, [])]}},
             2: {"data": {"totalCount": 3, "requisitions": [req(3, [{"city": "Austin", "state": "TX", "country": "US"}])]}}}
    calls = []

    def fake(url, data=None, headers=None, **kw):
        calls.append((url, data, headers))
        return HOME if data is None else pages[data["pageNumber"]]

    with mock.patch.object(js, "http", fake):
        jobs, complete = js.csod(dict(corp="acme"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert jobs[0]["title"] == "Job 1" and jobs[0]["location"] == "Cincinnati, OH, US" and jobs[0]["posted"] == "2026-09-04"
    assert jobs[0]["url"] == "https://acme.csod.com/ux/ats/careersite/1/home/requisition/1?c=acme" and jobs[0]["desc"] == "Do & things"
    assert calls[1][0] == "https://us.api.csod.com/rec-job-search/external/jobs" and calls[1][2]["Authorization"] == "Bearer TOK.EN"


def test_csod_missing_token_is_incomplete():
    with mock.patch.object(js, "http", lambda url, **kw: b"<html></html>"):
        assert js.csod(dict(corp="acme")) == ([], False)
