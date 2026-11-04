"""Offline shape tests for taleo_be, talentsoft and bpce: saved snippets of each site's markup/JSON stand in for the network."""
import json
from unittest import mock

import job_scout as js
from test_fetchers import fake_http


class FakeResp:
    def __init__(self, payload):
        self.payload = payload.encode() if isinstance(payload, str) else payload

    def read(self):
        return self.payload


class FakeOpener:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def open(self, req, timeout=None):
        self.calls.append(req)
        if "/jobs/search/" in req.full_url:
            return FakeResp('<input type = "hidden" name="tsstoken" id = "tsstoken" value ="TOK="/>')
        return FakeResp(json.dumps({"Result": self.pages[int(req.full_url.split("page_index=")[1].split("&")[0]) - 1]}))


def test_taleo_be_pages_token_and_rows():
    row = lambda i: (f'<div id="job_list_{i}" class="job_list_row"><p><a href="https://h/jobs/t-{i}" class="job_link font_bold">Title &amp; {i}</a></p>'
                     f'<span class="location"> Salt Lake City, Utah, United States </span></div>')
    pages = [f'<div id="jPaginateNumPages">2.0</div>{row(1)}{row(2)}', f'<div id="jPaginateNumPages">2.0</div>{row(3)}']
    op = FakeOpener(pages)
    with mock.patch.object(js.urllib.request, "build_opener", return_value=op):
        jobs, complete = js.taleo_be(dict(host="h", search_id="9", site_name="S"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert jobs[0]["title"] == "Title & 1" and jobs[0]["location"] == "Salt Lake City, Utah, United States"
    assert op.calls[-1].get_header("Tss-token") == "TOK="  # the AJAX layer rejects calls without it


def test_talentsoft_cards_and_stop_on_repeat():
    card = lambda i, city: (f'<div class="ts-offer-card Layer" onclick="x"><h3><a class="ts-offer-card__title-link  " href="/job/job-x-{i}_{i}.aspx" title="r">'
                            f' Analyst {i} </a></h3><ul><li>Permanent</li><li>United States Of America</li><li class="noBorder">{city}</li></ul></div>')
    seq = iter([(card(1, "NEW YORK") + card(2, "CHICAGO")).encode(), card(1, "NEW YORK").encode()])
    with mock.patch.object(js, "http", lambda url, **kw: next(seq)):
        jobs, complete = js.talentsoft(dict(host="j.example.com"))
    assert complete and [j["id"] for j in jobs] == ["1", "2"]
    assert jobs[0]["location"] == "NEW YORK, United States Of America" and jobs[0]["title"] == "Analyst 1"
    assert jobs[0]["url"] == "https://j.example.com/job/job-x-1_1.aspx"


def test_bpce_locations_dates_and_completeness():
    item = lambda n, city, region, country: {"job_number": n, "title": f"Job {n}", "date": "2026-10-09", "description": "<p>Desc</p>",
                                             "link": {"url": f"/en/job/j{n}"},
                                             "localisations": [{"city": city, "region": region, "country": country}]}
    data = {"data": {"total": 2, "items": [item("1", "New York", "United States", "International"), item("2", "Paris", "Ile-de-France", "France")]}}
    with mock.patch.object(js, "http", fake_http({"search/jobs": data})):
        jobs, complete = js.bpce(dict(host="r.example.com"))
    assert complete and [j["location"] for j in jobs] == ["New York, United States", "Paris, France"]
    assert jobs[0]["posted"] == "2026-10-09" and jobs[0]["desc"] == "Desc" and jobs[0]["url"] == "https://r.example.com/en/job/j1"
    data["data"]["total"] = 5
    with mock.patch.object(js, "http", fake_http({"search/jobs": data})):
        assert js.bpce(dict(host="r.example.com"))[1] is False  # fewer items than the site's total


def test_paylocity_page_data_and_dates():
    page = ('<script>window.pageData = {"Jobs": [{"JobId": 7, "JobTitle": "Teller", "LocationName": "Locust Valley", "PublishedDate": "2026-10-07T14:56:00-05:00",'
            ' "JobLocation": {"City": "Locust Valley", "State": "NY"}}]};\n</script>')
    with mock.patch.object(js, "http", fake_http({"paylocity.com/recruiting/jobs": page})):
        jobs, complete = js.paylocity(dict(board="https://recruiting.paylocity.com/recruiting/jobs/All/g/x"))
    assert complete and jobs == [dict(id="7", title="Teller", location="Locust Valley, NY",
                                      url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/7", posted="2026-10-07")]


def test_page_date_formats_and_describe_refines_posted():
    assert js._page_date('{"datePosted": "Oct 1, 2026"}') == "2026-10-01"
    assert js._page_date('{"datePosted":"2026-08-18T10:00:00Z"}') == "2026-08-18"
    assert js._page_date('{"datePosted": "soon"}') == "" and js._page_date("<html></html>") == ""
    page = '<script>{"datePosted": "Oct 1, 2026"}</script><body>Analyst role</body>'
    with mock.patch.object(js, "http", fake_http({"jobdetail": page})):
        text, posted, source = js.describe(dict(ats="kpmg"), dict(url="https://k/jobdetail/?jobId=1", id="1"))
    assert (posted, source) == ("2026-10-01", "exact") and "Analyst role" in text


def test_taleo_custom_column_order():
    page = {"requisitionList": [{"contestNo": "9", "column": ["9", "Risk Analyst ", json.dumps(["OH-Fairfield"])]}], "pagingData": {"pageSize": 25}}
    older = lambda body: page if not body["filterSelectionParam"]["searchFilterSelections"][0]["selectedValues"] else dict(page, requisitionList=[])
    with mock.patch.object(js, "http", fake_http({"searchjobs": older})):  # no job falls in an age bucket, so none gets a date
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="ex", portal="1", columns=[1, 2, -1]))
    assert complete and jobs[0]["title"] == "Risk Analyst" and jobs[0]["location"] == "OH-Fairfield" and jobs[0]["posted"] == ""


def test_parallel_terms_splits_merges_and_ands_completeness():
    calls = []

    @js._parallel_terms
    def fake(c):
        terms = js._terms(c)
        calls.append(terms)
        return [dict(id=t) for t in terms] + [dict(id="shared")], "d" not in terms

    c = dict(search=["x"], term_workers=2)
    with mock.patch.object(js, "TITLE_TERMS", ["a", "b", "c", "d"]):
        jobs, complete = fake(c)
    assert sorted(map(tuple, calls)) == [("a", "c"), ("b", "d")]  # round-robin chunks, every term searched once
    assert sorted(j["id"] for j in jobs) == ["a", "b", "c", "d", "shared"] and complete is False


def test_workday_fetches_later_pages_in_parallel_and_tracks_completeness():
    rows = lambda n, off: [dict(externalPath=f"/job/{off + i}", title=f"T{off + i}", locationsText="NY") for i in range(n)]

    def make(total, lose=None):
        def _http(url, data=None, headers=None, timeout=20, raw=False):
            off = data["offset"]
            if lose is not None and off == lose:
                raise js.urllib.error.HTTPError(url, 429, "slow down", {}, None)
            return {"total": total, "jobPostings": rows(max(0, min(20, total - off)), off)}
        return _http
    c = dict(host="h", tenant="t", site="s", search=["x"], max_per_term=100, term_ceiling=100, page_workers=3)
    with mock.patch.object(js, "http", make(70)):
        jobs, complete = js.workday(c)
    assert len(jobs) == 70 and complete  # 4 pages, last one short
    with mock.patch.object(js, "http", make(500)):
        jobs, complete = js.workday(c)
    assert len(jobs) == 100 and not complete  # stopped at the ceiling
    with mock.patch.object(js, "http", make(70, lose=40)):
        jobs, complete = js.workday(c)
    assert len(jobs) == 50 and not complete  # one page lost to a 429


def test_workday_shares_one_worker_pool_across_terms_and_pages():
    import threading
    import time
    state = dict(now=0, peak=0)
    lock = threading.Lock()

    def _http(url, data=None, headers=None, timeout=20, raw=False):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.01)
        with lock:
            state["now"] -= 1
        off, term = data["offset"], data["searchText"]
        total = 0 if term == "none" else 5000 if term == "" else 60  # the empty probe says big board
        n = max(0, min(20, total - off))
        return {"total": total, "jobPostings": [dict(externalPath=f"/{term}/{off + i}", title="T", locationsText="NY") for i in range(n)]}
    c = dict(host="h", tenant="t", site="s", search=["a", "b", "none"], max_per_term=100, term_ceiling=100, term_workers=2, page_workers=2,
             board_read_max=1000)  # a board over board_read_max falls back to the term search
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.workday(c)
    assert len(jobs) == 120 and complete  # the empty term costs one request and adds nothing
    assert 2 < state["peak"] <= 4  # more than term_workers, never above term_workers x page_workers


def _counting(handler):
    calls = []

    def _http(url, data=None, headers=None, timeout=20, raw=False):
        calls.append(url if data is None else (url, data))
        out = handler(url, data)
        return (out.encode() if isinstance(out, str) else out) if raw else out
    return _http, calls


def test_kpmg_reads_whole_board_in_parallel_pages_else_falls_back_to_terms():
    card = lambda i: f'<a href="/jobdetail/?jobId={i}"><div class="h5 text-dark-grey">Role {i}</div></a>'
    page = lambda pg, n, total: f'<span data-action="count">{total}</span> Results' + "".join(card(pg * 100 + k) for k in range(n))
    handler = lambda url, data: page(int(url.rsplit("spage=", 1)[1]), 12 if "spage=3" not in url else 5, 29)
    fake, calls = _counting(handler)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        jobs, complete = js.kpmg(dict(search=["x"]))
    assert complete and len(jobs) == 29 and len(calls) == 6 and all("keyword=&" in u for u in calls)  # empty-keyword board read twice (3 pages), not per term
    fake, calls = _counting(handler)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        js.kpmg(dict(search=["x"], full_board_max=10, term_workers=1))
    assert any("keyword=a" in u for u in calls) and any("keyword=b" in u for u in calls)  # board over the limit: every term still searched


def test_avature_results_board_stops_at_first_short_page_and_macquarie_prefetches_pages():
    card = lambda i: f'<article class="article--result "><a href="https://h/JobDetail/T-{i}/{i}">T {i}</a><span>Loc {i}</span></article>'
    off = lambda url: int(url.split("jobOffset=")[1])
    fake, calls = _counting(lambda url, data: "".join(card(i) for i in range(off(url), min(23, off(url) + 10))))
    c = dict(host="h", list_path="p", variant="results", search=["x"], page_workers=2)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        jobs, complete = js.avature(c)
    assert complete and len(jobs) == 23 and all("/p/?jobOffset=" in u for u in calls)  # blank search, no per-term path

    mq = lambda i: (f'<article class="article article--result"><a class="link" href="https://h/JobDetail?jobId={i}">Job {i}</a>'
                    f'<img alt="Office Location:"><p>Sydney</p></article>')
    fake, calls = _counting(lambda url, data: '<h2>25 results" x</h2>' + "".join(mq(i) for i in range(off(url), min(25, off(url) + 9))))
    with mock.patch.object(js, "http", fake):
        jobs, complete = js.avature(dict(host="h", list_path="p", variant="details", page_size=9))
    assert complete and len(jobs) == 25 and len(calls) == 3  # 9 + 9 + 7, none fetched twice


def test_eightfold_reads_whole_board_with_empty_query_else_searches_terms():
    start = lambda url: int(url.split("start=")[1].split("&")[0])
    board = lambda url, data: {"count": 25, "positions": [dict(id=i + 1, name=f"Job {i}", locations=["London"]) for i in range(start(url), min(25, start(url) + 10))]}
    fake, calls = _counting(board)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        jobs, complete = js.eightfold(dict(host="h", domain="d", search=["x"]))
    assert complete and len(jobs) == 25 and len(calls) == 3 and all(u.endswith("query=") for u in calls)
    fake, calls = _counting(board)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        js.eightfold(dict(host="h", domain="d", search=["x"], full_board_max=10, term_workers=1))
    assert {u.rsplit("query=", 1)[1] for u in calls} == {"", "a", "b"}  # the probe, then both terms


def test_goldman_reads_whole_board_at_page_size_200_else_searches_terms():
    role = lambda i: dict(roleId=f"R{i}_x", jobTitle=f"T{i}", descriptionHtml="<p>d</p>", lastPostedDate="2026-10-01", locations=[], externalSource=None)

    def gql(url, data):
        v = data["variables"]["searchQueryInput"]
        size, num = v["page"]["pageSize"], v["page"]["pageNumber"]
        return {"data": {"roleSearch": {"totalCount": 450, "items": [role(i) for i in range(num * size, min(450, (num + 1) * size))]}}}
    sizes = lambda calls: [d["variables"]["searchQueryInput"]["page"]["pageSize"] for _, d in calls]
    fake, calls = _counting(gql)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        jobs, complete = js.goldman(dict(search=["x"]))
    assert complete and len(jobs) == 450 and sizes(calls) == [1, 200, 200, 200]
    fake, calls = _counting(gql)
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b"]):
        js.goldman(dict(search=["x"], full_board_max=100))
    assert {d["variables"]["searchQueryInput"]["searchTerm"] for _, d in calls} == {"", "a", "b"}


def test_successfactors_skips_page_two_once_a_feed_is_seen_to_ignore_startrow():
    item = lambda i: f"<item><title>Role {i} (NY)</title><link>https://h/job/NY/Role/{i}/</link><description>d</description></item>"
    fake, calls = _counting(lambda url, data: "<rss><channel>" + "".join(item(i) for i in range(20)) + "</channel></rss>")  # startrow ignored
    js._SF_NO_PAGING.discard("h")
    with mock.patch.object(js, "http", fake), mock.patch.object(js, "TITLE_TERMS", ["a", "b", "c"]):
        jobs, complete = js.successfactors(dict(host="h", search=["x"], term_workers=1))
    js._SF_NO_PAGING.discard("h")
    assert complete and len(jobs) == 20 and len(calls) == 4  # term a: page 1 and the repeat; b and c: page 1 only
