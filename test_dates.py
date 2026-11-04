"""Offline tests for the real-posting-date paths added to describe() and the Taleo list: saved snippets stand in for the network."""
import json
from datetime import date, timedelta
from unittest import mock

import job_scout as js
from test_fetchers import fake_http


def _ld(stamp):
    return ('<html><script type="application/ld+json">' + json.dumps({"description": "<p>Role text</p>", "datePosted": stamp})
            + "</script></html>")


def test_icims_real_stamp_is_exact_and_synthetic_stamp_uses_sitemap():
    job = dict(id="13580", url="https://careers-x.icims.com/jobs/13580/analyst/job")
    with mock.patch.object(js, "http", fake_http({"/jobs/13580/": _ld("2026-10-09T04:00:00.000Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "2026-10-09", "exact")
    js._ICIMS_SITEMAPS.clear()
    sitemap = ('<urlset><url><loc>https://careers-x.icims.com/jobs/13580/analyst/job</loc><lastmod>2026-10-10T02:55:30-04:00</lastmod></url>'
               '<url><loc>https://careers-x.icims.com/jobs/1/other/job</loc><lastmod>2026-06-05T01:00:00-04:00</lastmod></url></urlset>')
    # East West Bank stamps datePosted with the render time minus two years; that must not become an "exact" date
    with mock.patch.object(js, "http", fake_http({"sitemap.xml": sitemap, "/jobs/13580/": _ld("2024-10-11T18:33:36.336Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "2026-10-10", "approx")
    js._ICIMS_SITEMAPS.clear()
    with mock.patch.object(js, "http", fake_http({"sitemap.xml": "<urlset/>", "/jobs/13580/": _ld("2024-10-11T18:33:36.336Z")})):
        assert js.describe(dict(ats="icims"), job) == ("Role text", "", "")
    js._ICIMS_SITEMAPS.clear()


def test_adp_rm_posting_date_from_search_meta():
    meta = {"jobRequisitions": [{"requisitionDescription": "<p>Credit role</p>", "postingInstructions": [
        {"timestampLastPosted": "2026-10-09T14:00:50Z", "postDate": ""}, {"timestampLastPosted": "2026-09-01T10:00:00Z"}]}]}
    routes = {"career-site": {"myJobsToken": "T"}, "search-meta/5001": meta}
    with mock.patch.object(js, "http", fake_http(routes)):
        # list jobs already carry desc: it is kept, only the date is added
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001", desc="From list")) == ("From list", "2026-10-09", "exact")
        # a queue entry has no desc: the endpoint's description is used
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001")) == ("Credit role", "2026-10-09", "exact")
    routes["search-meta/5001"] = {"jobRequisitions": [{"requisitionDescription": "x", "postingInstructions": [{"timestampLastPosted": ""}]}]}
    with mock.patch.object(js, "http", fake_http(routes)):
        assert js.describe(dict(ats="adp_rm", domain="d"), dict(id="5001")) == ("x", "", "")


def test_avature_feed_date_found_by_quoted_title():
    feed = ('<rss><channel><item><title>Other</title><link>https://h/careers/JobDetail/NY-Other/14000</link><pubDate>Wed, 27 Nov 2024 00:00:00 +0000</pubDate></item>'
            '<item><title>AI Fluency</title><link>https://h/careers/JobDetail/NY-AI-Fluency-Program-Manager/14291</link>'
            '<pubDate>Tue, 22 Sep 2026 00:00:00 +0000</pubDate></item></channel></rss>')
    seen = []

    def http(url, **kw):
        seen.append(url)
        return feed.encode() if "/feed/" in url else b"<html><body>Program role</body></html>"
    c = dict(ats="avature", host="h", list_path="careers/OpenRoles", feed=True)
    job = dict(id="14291", title="AI Fluency: Program Manager", url="https://h/careers/JobDetail/NY-AI/14291", posted="")
    with mock.patch.object(js, "http", http):
        text, posted, source = js.describe(c, job)
        assert (posted, source) == ("2026-09-22", "exact") and "Program role" in text
        assert any("search=%22AI%20Fluency%20%20Program%20Manager%22" in u for u in seen)  # punctuation dropped, title quoted
        assert js.describe(c, dict(job, id="99"))[1:] == ("", "")  # a job the feed does not list stays undated
        assert js.describe(dict(c, feed=False), job)[1:] == ("", "")  # tenants without a feed are untouched


def test_talentsoft_update_date_is_approx():
    page = ('<div class="ts-offer-page__maj-date"><h3> Date de mise à jour </h3> 09/10/2026 </div>'
            '<div class="ts-offer-page__body">Analyste role</div>')
    with mock.patch.object(js, "http", fake_http({"/job/": page})):
        text, posted, source = js.describe(dict(ats="talentsoft"), dict(id="1", url="https://j/job/x_1.aspx"))
    assert (posted, source) == ("2026-10-09", "approx") and "Analyste role" in text
    with mock.patch.object(js, "http", fake_http({"/job/": '<div class="ts-offer-page__body">No date</div>'})):
        assert js.describe(dict(ats="talentsoft"), dict(id="1", url="https://j/job/x_1.aspx"))[1:] == ("", "")


def test_taleo_without_date_column_buckets_ages():
    row = lambda n: {"contestNo": n, "column": [n, "Analyst " + n, json.dumps(["OH-Fairfield"])]}
    everything = [row("1"), row("2"), row("3"), row("4")]
    by_bucket = {"1": [], "2": [row("1")], "3": [row("1"), row("2")], "4": [row("1"), row("2"), row("3")],
                 "5": [row("1"), row("2"), row("3")], "6": [row("1"), row("2"), row("3")]}  # job 4 is older than 28 days

    def search(body):
        chosen = body["filterSelectionParam"]["searchFilterSelections"][0]["selectedValues"]
        return {"requisitionList": by_bucket[chosen[0]] if chosen else everything, "pagingData": {"pageSize": 25}}
    with mock.patch.object(js, "http", fake_http({"searchjobs": search})):
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="ex", portal="1", columns=[1, 2, -1]))
    today = date.today()
    assert complete and [(j["id"], j["posted"], j.get("posted_source")) for j in jobs] == [
        ("1", (today - timedelta(days=1)).isoformat(), "approx"),  # Yesterday bucket
        ("2", (today - timedelta(days=4)).isoformat(), "approx"),  # Last 7 Days: midpoint of 2..7
        ("3", (today - timedelta(days=11)).isoformat(), "approx"),  # Last 14 Days: midpoint of 8..14
        ("4", "", None)]


def test_taleo_age_bucket_failure_keeps_list_and_dates_found():
    def search(body):
        chosen = body["filterSelectionParam"]["searchFilterSelections"][0]["selectedValues"]
        if chosen and chosen[0] != "1":
            raise RuntimeError("500")
        return {"requisitionList": [{"contestNo": "1", "column": ["1", "T", "[]"]}], "pagingData": {"pageSize": 25}}
    with mock.patch.object(js, "http", fake_http({"searchjobs": search})):
        jobs, complete = js.taleo(dict(host="h", section="s", portal="1", columns=[1, 2, -1]))
    assert complete and jobs[0]["posted"] == date.today().isoformat()  # bucket 1 (Today) answered before the failure


def test_brassring_lastupdated_is_labeled_approx():
    from types import SimpleNamespace
    job = {"Link": "https://j/x", "Questions": [{"QuestionName": k, "Value": v} for k, v in
                                                  dict(reqid="9", lastupdated="11-Oct-2026", jobtitle="Quant", formtext23="UK", jobdescription="<p>D</p>").items()]}

    class Opener:
        def open(self, req, timeout=None):
            body = {"JobsCount": 1, "Jobs": {"Job": [job]}}
            return SimpleNamespace(read=lambda: b'<input name="__RequestVerificationToken" value="R">' if "HomeWithPreLoad" in req.full_url
                                   else json.dumps(body).encode())
    with mock.patch.object(js.urllib.request, "build_opener", return_value=Opener()), \
            mock.patch("http.cookiejar.CookieJar", lambda: [SimpleNamespace(name="tg_session_1_2", value="S")]):
        jobs, complete = js.brassring(dict(host="j", partnerid=1, siteid=2))
    assert complete and (jobs[0]["posted"], jobs[0]["posted_source"]) == ("2026-10-11", "approx")  # an update date, not a posting date


def test_retry_enrichment_dates_undated_detail_ats_entries(monkeypatch):
    monkeypatch.setattr(js, "describe", lambda c, j: ("text", "2026-10-09", "approx"))
    e = dict(id="Valley:5001", company="Valley", link="https://x", role="Analyst", location="NY", posted="", state="new",
             description_status="ok", site=None)
    js._retry_enrichment([e], dict(companies=[dict(name="Valley", ats="adp_rm")]), [])
    assert (e["posted"], e["posted_source"]) == ("2026-10-09", "approx")


def test_bloomberg_dates_come_from_its_searchjobs_feed():
    c = next(x for x in json.load(open("data/config.json"))["companies"] if x["name"] == "Bloomberg")
    assert c.get("feed") and c["list_path"] == "careers/SearchJobs"  # Bloomberg pages carry no JSON-LD; the feed has the posting day
    feed = ('<rss><channel><item><title>eFX Sales</title><link>https://bloomberg.avature.net/careers/JobDetail/eFX-Sales/45118</link>'
            '<pubDate>Tue, 06 Oct 2026 00:00:00 +0000</pubDate></item></channel></rss>')
    seen = []

    def http(url, **kw):
        seen.append(url)
        return feed.encode() if "/feed/" in url else b"<html><body>Sales role</body></html>"
    job = dict(id="45118", title="eFX Sales", url="https://bloomberg.avature.net/careers/JobDetail/eFX-Sales/45118", posted="")
    with mock.patch.object(js, "http", http):
        assert js.describe(c, job)[1:] == ("2026-10-06", "exact")
    assert any(u.startswith("https://bloomberg.avature.net/careers/SearchJobs/feed/?search=") for u in seen)


# id interpolation fallback (estimate_posted_from_id / _fill_dates_from_id)

_ANCH = [("100", "2026-09-01"), ("110", "2026-09-11"), ("120", "2026-09-21"), ("130", "2026-10-01"), ("140", "2026-10-06"), ("150", "2026-10-09")]


def test_id_interpolation_between_neighbours_and_cap():
    assert js.estimate_posted_from_id("115", "2026-10-11", _ANCH) == "2026-09-16"
    assert js.estimate_posted_from_id("145", "2026-10-11", _ANCH) == "2026-10-08"  # 2026-10-07.5 rounds to the even ordinal
    assert js.estimate_posted_from_id("115", "2026-09-10", _ANCH) == "2026-09-10"  # never later than the seed run


def test_id_interpolation_refuses_without_two_neighbours_or_clean_input():
    assert js.estimate_posted_from_id("95", "2026-10-11", _ANCH) == ""  # only an upper bound
    assert js.estimate_posted_from_id("155", "2026-10-11", _ANCH) == "2026-10-09"  # lower bound alone, predates seed
    assert js.estimate_posted_from_id("155", "2026-10-05", _ANCH) == ""  # lower bound after the seed run: contradiction
    assert js.estimate_posted_from_id("120", "2026-10-11", _ANCH) == ""  # id is itself an anchor
    assert js.estimate_posted_from_id("REQ-7", "2026-10-11", _ANCH) == ""  # non-numeric id
    assert js.estimate_posted_from_id("115", "2026-10-11", _ANCH[:4]) == ""  # too few anchors
    assert js.estimate_posted_from_id("115", "2026-10-11", [(i, "2026-09-05") for i, _ in _ANCH]) == ""  # all anchors on one day: no id-to-date evidence
    shuffled = [("100", "2026-10-01"), ("110", "2026-09-05"), ("120", "2026-09-28"), ("130", "2026-09-02"), ("140", "2026-10-06"), ("150", "2026-09-09")]
    assert js.estimate_posted_from_id("115", "2026-10-11", shuffled) == ""  # ids are not monotonic in time here


def test_fill_dates_from_id_never_overrides_real_dates():
    def row(jid, **kw):
        base = dict(id=f"Co:{jid}", company="Co", first_seen="2026-10-11", first_run=True, posted="", posted_source="")
        return dict(base, **kw)
    queue = [row("100", posted="2026-09-01", posted_source="exact"), row("110", posted="2026-09-11", posted_source="exact"),
             row("120", posted="2026-09-21", posted_source="exact"), row("130", posted="2026-10-01", posted_source="exact"),
             row("140", first_seen="2026-10-12", first_run=False), row("150", first_seen="2026-10-14", first_run=False),
             row("105"), row("125", posted="2026-09-24", posted_source="exact")]
    cfg = dict(companies=[dict(name="Co", ats="x", date_from_id=True), dict(name="Other", ats="x")])
    assert js._fill_dates_from_id(queue, cfg) == 1
    est = queue[6]
    assert (est["posted"], est["posted_source"], est["posted_est"]) == ("2026-09-06", "approx", True)
    assert queue[7]["posted"] == "2026-09-24" and "posted_est" not in queue[7]
    assert js.posted_label(est) == "~2026-09-06"
    cfg["companies"][0]["date_from_id"] = False
    queue[6]["posted"] = ""
    assert js._fill_dates_from_id(queue, cfg) == 0 and queue[6]["posted"] == ""


def test_workday_full_location_lists_every_site():
    assert js._workday_full_location({"location": "PA - Pittsburgh (15222)", "additionalLocations": ["NY - New York (10022)", "PA - Pittsburgh (15222)"]}) == "PA - Pittsburgh (15222); NY - New York (10022)"
    assert js._workday_full_location({}) == ""


def test_rescue_multi_site_rechecks_full_location(monkeypatch, tmp_path):
    monkeypatch.setattr(js, "MULTISITE_CACHE", tmp_path / "c.json")
    filt = {"title_include": ["treasury"], "title_exclude": [], "locations_include": [], "locations_exclude": ["pittsburgh"]}
    jobs = [dict(id="/j/1", title="Treasury Analyst", location="2 Locations (PA Pittsburgh 15222)", site="S"),
            dict(id="/j/2", title="Chef", location="2 Locations (PA Pittsburgh 15222)", site="S")]

    def fake(c, job):
        job["location"] = "NY - New York (10022)"

    monkeypatch.setattr(js, "describe", fake)
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    got = js._rescue_multi_site({"name": "X", "ats": "workday"}, jobs, [], filt)
    assert [j["id"] for j in got] == ["/j/1"] and got[0]["location"] == "NY - New York (10022)"
    assert js._rescue_multi_site({"name": "X", "ats": "greenhouse"}, jobs, [], filt) == []
