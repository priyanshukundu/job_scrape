"""Offline tests for the 2026-10-12 fetchers (Eightfold PCSX, Rippling, CareerPuck, Sitecore Search): canned responses stand in for http()."""
import json
from unittest import mock

import job_scout as js
from test_fetchers import fake_http

QC = dict(host="careers.qualcomm.com", domain="qualcomm.com", pcsx=True)


def pcsx_row(i, **kw):
    base = {"id": i, "name": f"Role {i}", "locations": ["San Diego, CA, United States"], "postedTs": 1791763200,
            "creationTs": 1772668800, "positionUrl": f"/careers/job/{i}"}
    base.update(kw)
    return base


def test_eightfold_pcsx_search_url_dates_and_overlap():
    seen = []

    def _http(url, **kw):
        seen.append(url)
        start = int(url.split("start=")[1].split("&")[0])
        rows = [pcsx_row(100 + start // 10 * 10 + k) for k in range(10)]
        if start == 10:
            rows[0] = pcsx_row(100)  # PCSX pages overlap on tied sort stamps: the duplicate is dropped
        return {"data": {"count": 20, "positions": rows}}
    with mock.patch.object(js, "http", _http):
        jobs, complete = js._eightfold_board(QC)
    assert complete and len(jobs) == 19  # 20 listed, 19 distinct, still complete
    assert all("/api/pcsx/search?" in u and "domain=qualcomm.com" in u for u in seen)
    assert jobs[0]["posted"] == "2026-03-05"  # creationTs, not the daily-refreshed postedTs
    assert jobs[0]["url"] == "https://careers.qualcomm.com/careers/job/100"


def test_eightfold_pcsx_describe_and_non_pcsx_unchanged():
    with mock.patch.object(js, "http", fake_http({"/api/pcsx/position_details": {"data": {"jobDescription": "<p>Build <b>chips</b></p>"}}})):
        assert js.describe(dict(QC, ats="eightfold"), {"id": "9"}) == ("Build chips", "", "")
    with mock.patch.object(js, "http", fake_http({"/api/apply/v2/jobs/9": {"job_description": "<p>Old style</p>"}})):
        assert js.describe({"ats": "eightfold", "host": "h", "domain": "d"}, {"id": "9"})[0] == "Old style"


def test_rippling_pages_details_dates_remote():
    listing = {"items": [{"id": "a", "name": " Analyst ", "url": "https://ats.rippling.com/x/jobs/a",
                          "locations": [{"name": "New York, NY", "workplaceType": "ON_SITE"}]},
                         {"id": "b", "name": "Quant", "locations": [{"name": "Remote, US", "workplaceType": "REMOTE"}]}],
               "totalItems": 2, "totalPages": 1}
    det = {"createdOn": "2026-09-02T12:28:00.586000-07:00", "description": {"company": "<p>Co</p>", "role": "<p>Do <b>it</b></p>"}}
    with mock.patch.object(js, "http", fake_http({"/jobs/a": det, "/jobs/b": dict(det, createdOn=""), "pageSize=100": listing})):
        jobs, complete = js.rippling(dict(slug="x"))
    assert complete and [j["id"] for j in jobs] == ["a", "b"]
    assert jobs[0]["title"] == "Analyst" and jobs[0]["posted"] == "2026-09-02" and jobs[0]["desc"] == "Co Do it"
    assert jobs[1]["location"] == "Remote, US | Remote" and jobs[1]["posted"] == ""
    assert jobs[1]["url"] == "https://ats.rippling.com/x/jobs/b"


def test_rippling_describe_returns_exact_date():
    det = {"createdOn": "2026-05-27T08:00:00-07:00", "description": {"role": "<p>Text</p>"}}
    with mock.patch.object(js, "http", fake_http({"/board/webull/jobs/u1": det})):
        assert js.describe({"ats": "rippling", "slug": "webull"}, {"id": "u1"}) == ("Text", "2026-05-27", "exact")


def careerpuck_board():
    return {"jobs": [
        {"permalink": "p1", "status": "public", "title": "AVP, Risk ", "location": "Jersey City", "atsSourceId": "55",
         "content": "&lt;p&gt;Fortitude&period; Re &amp;amp&semi; more&lt;/p&gt;", "salaryDescription": "Base $100k",
         "publicUrl": "https://app.careerpuck.com/job-board/b/job/55", "postedAt": "2026-07-23T17:03:34.711Z", "offices": [{"name": "US-Jersey City"}]},
        {"permalink": "p2", "status": "draft", "title": "Hidden", "location": "X"},
        {"permalink": "p3", "status": "public", "title": "Remote one", "location": None, "offices": [{"name": "Remote-US"}], "content": "", "postedAt": None}]}


def test_careerpuck_single_call_fields_and_entity_decoding():
    with mock.patch.object(js, "http", fake_http({"/v1/public/job-boards/b": careerpuck_board()})):
        jobs, complete = js.careerpuck(dict(slug="b"))
    assert complete and [j["id"] for j in jobs] == ["p1", "p3"]
    assert jobs[0]["title"] == "AVP, Risk" and jobs[0]["location"] == "Jersey City" and jobs[0]["posted"] == "2026-07-23"
    assert jobs[0]["desc"] == "Fortitude. Re & more Base $100k"
    assert jobs[1]["location"] == "Remote-US" and jobs[1]["posted"] == "" and jobs[1]["url"].endswith("/job/p3")


def test_careerpuck_describe_picks_row_from_board():
    with mock.patch.object(js, "http", fake_http({"/v1/public/job-boards/b": careerpuck_board()})):
        text, posted, src = js.describe({"ats": "careerpuck", "slug": "b"}, {"id": "p1"})
        assert text.startswith("Fortitude.") and (posted, src) == ("", "")
        assert js.describe({"ats": "careerpuck", "slug": "b"}, {"id": "gone"}) == ("", "", "")


def sitecore_row(i, **kw):
    base = {"job_id": str(i), "id": f"h{i}", "name": f"Role {i}", "job_primary_city": "Atlanta", "job_primary_region": "GA",
            "job_primary_country": "United States", "job_url": f"https://careers.kroll.com/en/job/x/{i}",
            "posted_date": "2026-09-11T10:00:00Z", "job_description": "<p>Value <b>things</b></p>", "working_mode": "Hybrid"}
    base.update(kw)
    return base


def test_sitecore_search_pages_by_offset_and_maps_fields():
    bodies = []
    pages = iter([{"widgets": [{"total_item": 3, "content": [sitecore_row(1), sitecore_row(2, working_mode="Remote")]}]},
                  {"widgets": [{"total_item": 3, "content": [sitecore_row(3, job_primary_region="", job_primary_country="")]}]}])

    def _http(url, data=None, headers=None, **kw):
        bodies.append((url, data, headers))
        return next(pages)
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.sitecore_search(dict(discover_id="123", api_key="KEY", entity="careersprod"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert bodies[0][0] == "https://discover.sitecorecloud.io/discover/v2/123" and bodies[0][2] == {"Authorization": "KEY"}
    item = bodies[1][1]["widget"]["items"][0]
    assert item["rfk_id"] == "rfkid_7" and item["entity"] == "careersprod" and item["search"]["offset"] == 2
    assert jobs[0]["location"] == "Atlanta, GA, United States" and jobs[1]["location"].endswith("| Remote") and jobs[2]["location"] == "Atlanta"
    assert jobs[0]["posted"] == "2026-09-11" and jobs[0]["desc"] == "Value things"


def test_sitecore_search_incomplete_when_rows_missing():
    calls = iter([{"widgets": [{"total_item": 5, "content": [sitecore_row(1)]}]}, {"widgets": [{"total_item": 5, "content": []}]}])
    with mock.patch.object(js, "http", lambda *a, **k: next(calls)):
        jobs, complete = js.sitecore_search(dict(discover_id="1", api_key="k", entity="e"))
    assert len(jobs) == 1 and not complete


def test_sitecore_search_describe_picks_row():
    rows = {"widgets": [{"total_item": 1, "content": [sitecore_row(7)]}]}
    with mock.patch.object(js, "http", lambda *a, **k: rows):
        c = dict(ats="sitecore_search", discover_id="1", api_key="k", entity="e")
        assert js.describe(c, {"id": "7"})[0] == "Value things"


def test_bamboohr_list_detail_date_and_describe():
    listing = {"result": [{"id": "121", "jobOpeningName": " Manager ", "location": {"city": "New York", "state": "New York"}, "isRemote": None},
                          {"id": "119", "jobOpeningName": "QA", "location": {"city": None, "state": None}, "isRemote": True}]}
    det = {"result": {"jobOpening": {"datePosted": "2026-10-16", "description": "<p>Do <b>it</b></p>"}}}
    with mock.patch.object(js, "http", fake_http({"/careers/121/detail": det, "/careers/119/detail": {"result": {"jobOpening": {}}}, "/careers/list": listing})):
        jobs, complete = js.bamboohr(dict(slug="x"))
        assert js.describe({"ats": "bamboohr", "slug": "x"}, {"id": "121"}) == ("Do it", "2026-10-16", "exact")
    assert complete and jobs[0]["title"] == "Manager" and jobs[0]["location"] == "New York, New York" and jobs[0]["posted"] == "2026-10-16"
    assert jobs[1]["location"] == "Remote" and jobs[1]["posted"] == "" and jobs[0]["url"] == "https://x.bamboohr.com/careers/121"
