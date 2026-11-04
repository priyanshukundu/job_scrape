"""Offline shape tests for the 2026-10 fetchers: canned responses stand in for http(), so a parser regression fails here, not silently in production."""
import json
from unittest import mock

import job_scout as js


def fake_http(routes):
    def _http(url, data=None, headers=None, timeout=20, raw=False):
        for key, payload in routes.items():
            if key in url:
                if raw:
                    return payload if isinstance(payload, bytes) else payload.encode()
                return payload(data) if callable(payload) else payload
        raise AssertionError(f"unexpected url {url}")
    return _http


def test_taleo_pages_and_dates():
    page1 = {"requisitionList": [{"contestNo": "A1", "column": ["Analyst ", json.dumps(["US-NY-New York"]), "Oct 2, 2026"]}] * 1 + [
        {"contestNo": f"B{i}", "column": ["T", "[]", "Oct 1, 2026"]} for i in range(24)], "pagingData": {"pageSize": 25}}
    page2 = {"requisitionList": [{"contestNo": "C1", "column": ["Last", "[]", "bad date"]}], "pagingData": {"pageSize": 25}}
    pages = iter([page1, page2])
    with mock.patch.object(js, "http", fake_http({"searchjobs": lambda body: next(pages)})):
        jobs, complete = js.taleo(dict(host="h.taleo.net", section="s", portal="1"))
    assert complete and len(jobs) == 26
    assert jobs[0] == dict(id="A1", title="Analyst", location="US-NY-New York",
                           url="https://h.taleo.net/careersection/s/jobdetail.ftl?job=A1&lang=en", posted="2026-10-02")
    assert jobs[-1]["posted"] == ""


def test_hrmdirect_parses_list_and_detail():
    lst = '<a href="job-opening.php?req=11&req_loc=1&&amp;nohd#job">Compliance Analyst</a><a href="job-opening.php?req=11&&amp;nohd#job">Compliance Analyst</a>'
    detail = "<html><body>Compliance Analyst Location: New York, NY Type of Hire: Experienced Hires Division: X</body></html>"
    with mock.patch.object(js, "http", fake_http({"job-openings.php": lst, "job-opening.php": detail})):
        jobs, complete = js.hrmdirect(dict(host="opco.hrmdirect.com"))
    assert complete and len(jobs) == 1
    assert jobs[0]["id"] == "11" and jobs[0]["location"] == "New York, NY" and jobs[0]["posted"] == ""


def test_commerzbank_paging():
    item = lambda i: {"MatchedObjectId": str(i), "MatchedObjectDescriptor": {
        "ID": str(i), "PositionTitle": f"Job {i}", "PositionURI": f"u{i}", "PublicationStartDate": "2026-10-02",
        "PositionLocation": [{"CityName": "Frankfurt", "CountryName": "Germany"}]}}
    batches = iter([{"SearchResult": {"SearchResultItems": [item(i) for i in range(100)]}},
                    {"SearchResult": {"SearchResultItems": [item(100)]}}])
    with mock.patch.object(js, "http", fake_http({"api-jobs.commerzbank.com": lambda body: next(batches)})):
        jobs, complete = js.commerzbank(dict())
    assert complete and len(jobs) == 101
    assert jobs[0]["location"] == "Frankfurt, Germany" and jobs[0]["posted"] == "2026-10-02"


def test_dzbank_dates_and_desc():
    rows = [dict(jobReqId="1", stellenbezeichnung="T", standort="Singapore, SG", joblink="https://x/1", publiziert="01.10.2026", beschreibung="<p>Hi</p>"),
            dict(jobReqId="", stellenbezeichnung="skipped", joblink="")]
    with mock.patch.object(js, "http", fake_http({"sapjobs.json": rows})):
        jobs, complete = js.dzbank(dict())
    assert complete and len(jobs) == 1
    assert jobs[0]["posted"] == "2026-10-01" and jobs[0]["desc"] == "Hi"


def test_marketaxess_joins_dates_to_posts():
    jobs_json = [{"id": 5, "opened_at": "2026-09-22T09:23:00-04:00"}]
    posts = [{"job_id": 5, "title": "Research Analyst, ML", "location": {"name": "New York"}, "content": "<p>Role</p>"}]
    with mock.patch.object(js, "http", fake_http({"path=/jobs/": jobs_json, "path=/job_posts/": posts})):
        jobs, complete = js.marketaxess(dict())
    assert complete and jobs[0]["posted"] == "2026-09-22" and jobs[0]["url"].endswith("/research-analyst-ml-5")


def test_avature_details_variant():
    card = ('<div class="article article--result clearfix "><a class="link" href="https://r.example/en_US/careers/JobDetail?jobId=77"> Treasury Analyst </a>'
            '<img alt="Office Location:" /> <p> Sydney Office </p><img alt="Posted Date:" /> <p> 02 Oct 2026 </p></article>')
    with mock.patch.object(js, "http", fake_http({"SearchJobs": "<html>" + card + "</html>"})):
        jobs, complete = js.avature(dict(host="r.example", list_path="en_US/careers/SearchJobs", variant="details", page_size=9))
    assert complete
    assert jobs == [dict(id="77", title="Treasury Analyst", url="https://r.example/en_US/careers/JobDetail?jobId=77",
                         location="Sydney Office", posted="2026-10-02")]


def test_successfactors_pages_past_a_page_that_only_repeats_earlier_searches():
    def item(i):
        return f"<item><title>Job {i} (NY)</title><link>https://h/job/x/{i}/</link><pubDate>Fri, 02 Oct 2026 10:00:00 GMT</pubDate><description>d</description></item>"
    def feed(ids):
        return ("<rss><channel>" + "".join(item(i) for i in ids) + "</channel></rss>").encode()
    pages = {("(a)", 0): feed(range(20)), ("(a)", 20): feed([]), ("(b)", 0): feed(range(20)), ("(b)", 20): feed(range(20, 25))}

    def _http(url, data=None, headers=None, timeout=20, raw=False):
        q = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))
        return pages[(js.urllib.parse.unquote(q["keywords"]), int(q["startrow"]))]
    with mock.patch.object(js, "http", _http):
        jobs, complete = js.successfactors(dict(host="h", locale="en_US", search=["a", "b"], max_per_term=100))
    assert complete and len(jobs) == 25


def _old_entry(company, **kw):
    e = dict(id=f"{company}:1", company=company, state="new", posted="2020-01-01", last_seen_live="2020-01-02", closed_on=None)
    e.update(kw)
    return e


def test_prune_skips_unverified_companies_but_prunes_clean_ones():
    queue = [_old_entry("Flaky", closed_on="2026-10-02"), _old_entry("Clean", closed_on="2026-10-02"), _old_entry("Gone", closed_on="2026-10-02")]
    counts = {}
    js._prune_stale(queue, counts, unverified={"Flaky"})
    assert [e["company"] for e in queue] == ["Flaky"] and counts["pruned"] == 2


def test_prune_tolerates_null_posted():
    queue = [_old_entry("A", posted=None)]
    js._prune_stale(queue, {}, unverified=set())
    assert len(queue) == 1


def test_run_writes_queue_before_state(tmp_path, monkeypatch):
    import argparse
    (tmp_path / "config.json").write_text(json.dumps(dict(notify={}, filters={}, keywords=[], companies=[])))
    monkeypatch.setattr(js, "HOME", tmp_path)
    order = []
    monkeypatch.setattr(js.atomic_json, "write", lambda path, data: order.append(path.replace(chr(92), "/").rsplit("/", 1)[-1]))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda q, c: None)
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))
    assert order == ["queue.json", "state.json"]


def _scout(tmp_path, monkeypatch, jobs, state=None, cap=None, queue=None, notify_ok=True):
    import argparse
    (tmp_path / "config.json").write_text(json.dumps(dict(notify={}, filters=dict(title_include=["treasury"]), keywords=[],
                                                         companies=[dict(name="X", ats="fake")])))
    if state is not None:
        (tmp_path / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setitem(js.FETCH, "fake", lambda c: (jobs, True))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda q, c: None)
    if queue is not None:
        (tmp_path / "queue.json").write_text(json.dumps(queue))
    if cap is not None:
        monkeypatch.setattr(js, "ALERT_CAP_PER_RUN", cap)
    sent = []
    monkeypatch.setattr(js, "notify", lambda cfg, entry, dry: sent.append(entry) or notify_ok)
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))
    queue = json.loads((tmp_path / "queue.json").read_text())
    state_out = json.loads((tmp_path / "state.json").read_text())
    return sent, queue, state_out


def _job(i):
    return dict(id=str(i), title=f"Treasury Analyst {i}", location="New York, NY", url=f"https://x/{i}", posted="2026-10-01")


def test_flood_guard_queues_silently_with_one_summary(tmp_path, monkeypatch):
    sent, queue, _ = _scout(tmp_path, monkeypatch, [_job(i) for i in range(15)], state={"X": ["old"]})
    assert len(queue) == 15
    assert [e["company"] for e in sent] == ["Summary"] and "15 roles appeared at once" in sent[0]["role"]


def test_alert_cap_rolls_overflow_into_one_summary(tmp_path, monkeypatch):
    old = [_job(i) for i in range(20)]
    sent, queue, _ = _scout(tmp_path, monkeypatch, old + [_job(100 + i) for i in range(3)], state={"X": [str(i) for i in range(20)]}, cap=2)
    assert len(queue) == 3
    assert [e["company"] for e in sent] == ["X", "X", "Summary"] and "1 more new roles" in sent[2]["role"]


def test_empty_first_fetch_does_not_seed_state(tmp_path, monkeypatch):
    sent, queue, state = _scout(tmp_path, monkeypatch, [])
    assert "X" not in state and not sent and queue == []


def test_repeated_id_in_one_fetch_queues_once(tmp_path, monkeypatch):
    _, queue, _ = _scout(tmp_path, monkeypatch, [_job(1), _job(1)])
    assert len(queue) == 1


def test_failed_alert_is_marked_pending_then_retried_then_dropped(tmp_path, monkeypatch):
    state = {"X": [str(i) for i in range(20)]}
    old = [_job(i) for i in range(20)]
    # run 1: delivery fails -> entry queued with alert_pending
    sent, queue, _ = _scout(tmp_path, monkeypatch, old + [_job(100)], state=state, notify_ok=False)
    new = [e for e in queue if e["id"] == "X:100"][0]
    assert new["alert_pending"] and new["alert_attempts"] == 1
    # run 2: delivery works -> pending cleared, counted once
    sent, queue, _ = _scout(tmp_path, monkeypatch, old + [_job(100)], state=state, queue=queue, notify_ok=True)
    assert [e["id"] for e in sent] == ["X:100"]
    assert not [e for e in queue if e["id"] == "X:100"][0]["alert_pending"]


def test_pending_alert_gives_up_after_max_attempts(tmp_path, monkeypatch):
    entry = dict(id="X:9", company="X", role="Treasury Analyst", location="", link="u", sponsorship_status="Unclear", kw_hits=[],
                 state="new", closed_on=None, alert_pending=True, alert_attempts=js.ALERT_MAX_ATTEMPTS)
    sent, queue, _ = _scout(tmp_path, monkeypatch, [_job(9)], state={"X": ["9"]}, queue=[entry], notify_ok=False)
    assert not sent and not queue[0]["alert_pending"]


def _liveness(last_seen, misses):
    e = dict(id="X:1", company="X", closed_on=None, miss_count=misses, last_seen_live=last_seen, state="new")
    counts = dict(closed=0, reopened=0)
    js._update_liveness("X", [dict(id="other")], True, [e], counts)
    return e


def test_closure_waits_for_a_day_after_last_sighting():
    from datetime import date, timedelta
    today = date.today().isoformat()
    assert _liveness(today, 5)["closed_on"] is None  # seen earlier today: enough misses, still too soon
    assert _liveness((date.today() - timedelta(days=1)).isoformat(), 1)["closed_on"] == today
    assert _liveness((date.today() - timedelta(days=1)).isoformat(), 0)["closed_on"] is None  # needs two misses


def test_prune_writes_tombstones_and_recall_skips_them(tmp_path, monkeypatch):
    import argparse
    monkeypatch.setattr(js, "HOME", tmp_path)
    queue = [_old_entry("X", closed_on="2026-10-02")]
    counts = {}
    js._prune_stale(queue, counts, unverified=set())
    js._write_tombstones(counts["pruned_ids"], "pruned")
    assert js._load_tombstones() == {"X:1"}


def _recall(tmp_path, monkeypatch, jobs, seen, queue=None, tomb=(), dry=False, cap=None):
    import argparse
    (tmp_path / "config.json").write_text(json.dumps(dict(notify={}, filters=dict(title_include=["treasury"]), keywords=[],
                                                         companies=[dict(name="X", ats="fake")])))
    (tmp_path / "state.json").write_text(json.dumps({"X": seen}))
    if queue is not None:
        (tmp_path / "queue.json").write_text(json.dumps(queue))
    if tomb:
        js.HOME = tmp_path
        js._write_tombstones(list(tomb), "purged")
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setitem(js.FETCH, "fake", lambda c: (jobs, True))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda q, c: None)
    if cap is not None:
        monkeypatch.setattr(js, "RECALL_MAX", cap)
    sent = []
    monkeypatch.setattr(js, "notify", lambda cfg, entry, dry: sent.append(entry) or True)
    js.run(argparse.Namespace(from_fetched=None, dry_run=dry, check=False, recall=True))
    q = json.loads((tmp_path / "queue.json").read_text()) if (tmp_path / "queue.json").exists() else []
    return sent, q


def test_recall_queues_seen_unqueued_matches_silently_and_tags_origin(tmp_path, monkeypatch):
    sent, q = _recall(tmp_path, monkeypatch, [_job(1), _job(2)], seen=["1", "2"], tomb=["X:2"])
    assert [e["id"] for e in q] == ["X:1"] and q[0]["origin"] == "recall" and not sent


def test_recall_respects_cap_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    _, q = _recall(tmp_path, monkeypatch, [_job(i) for i in range(5)], seen=[str(i) for i in range(5)], cap=3)
    assert len(q) == 3
    _, q2 = _recall(tmp_path / "d" if (tmp_path / "d").mkdir() is None else tmp_path, monkeypatch, [_job(1)], seen=["1"], dry=True)
    assert q2 == []


def _enrich_entry(**kw):
    e = dict(id="X:7", company="X", role="Treasury Analyst", location="NY", link="https://x/7", state="new", closed_on=None,
             posted="2026-10-01", sponsorship_status="Unclear", sponsorship_evidence="", experience=None, experience_evidence="",
             salary=None, salary_evidence="", kw_hits=[], description_status="failed")
    e.update(kw)
    return e


def test_retry_recovers_failed_description_and_reclassifies(monkeypatch):
    monkeypatch.setattr(js, "describe", lambda c, j: ("We do not sponsor visas. 3+ years of experience.", "", ""))
    e = _enrich_entry()
    js._retry_enrichment([e], dict(companies=[dict(name="X", ats="fake")]), [])
    assert e["description_status"] == "ok" and e["sponsorship_status"] == "OPT Only" and e["state"] == "opt_only"


def test_retry_hard_block_still_auto_blocks(monkeypatch):
    monkeypatch.setattr(js, "describe", lambda c, j: ("Must be a U.S. citizen. We do not sponsor visas.", "", ""))
    e = _enrich_entry()
    js._retry_enrichment([e], dict(companies=[dict(name="X", ats="fake")]), [])
    assert e["sponsorship_status"] == "Blocked" and e["state"] == "auto_blocked"


def test_retry_backs_off_one_day_and_stops_after_max_attempts(monkeypatch):
    calls = []

    def boom(c, j):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(js, "describe", boom)
    cfg = dict(companies=[dict(name="X", ats="fake")])
    e = _enrich_entry()
    js._retry_enrichment([e], cfg, [])
    js._retry_enrichment([e], cfg, [])  # same day: enrich_next is tomorrow, so no second call
    assert len(calls) == 1 and e["enrich_attempts"] == 1
    e2 = _enrich_entry(enrich_attempts=js.ENRICH_MAX_ATTEMPTS)
    js._retry_enrichment([e2], cfg, [])
    assert len(calls) == 1


def test_retry_fills_undated_workday_entry(monkeypatch):
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    monkeypatch.setattr(js, "describe", lambda c, j: ("text", "2026-09-30", "exact"))
    e = _enrich_entry(posted="", description_status="ok")
    js._retry_enrichment([e], dict(companies=[dict(name="X", ats="workday")]), [])
    assert e["posted"] == "2026-09-30" and e["posted_source"] == "exact"


def test_failed_description_at_intake_is_flagged(tmp_path, monkeypatch):
    def bad(c, j):
        raise RuntimeError("down")
    monkeypatch.setattr(js, "describe", bad)
    _, queue, _ = _scout(tmp_path, monkeypatch, [_job(1)])
    assert queue[0]["description_status"] == "failed"


def test_prune_requires_confirmed_closure_but_drops_companies_removed_from_config():
    open_old = _old_entry("Live")  # old posting, not closed: a single complete miss must not prune it
    removed = _old_entry("NoLongerTracked")
    queue = [open_old, removed]
    js._prune_stale(queue, {}, unverified=set(), configured={"Live"})
    assert [e["company"] for e in queue] == ["Live"]


def test_failed_summary_is_kept_and_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setattr(js, "notify", lambda cfg, entry, dry: False)
    js._send_summaries({}, [("X", 15)], {}, False)
    assert json.loads((tmp_path / "pending_summaries.json").read_text()) == [dict(line="X: 15 roles appeared at once, queued silently (flood guard)", attempts=1)]
    sent = []
    monkeypatch.setattr(js, "notify", lambda cfg, entry, dry: sent.append(entry["role"]) or True)
    js._send_summaries({}, [], {}, False)
    assert sent == ["X: 15 roles appeared at once, queued silently (flood guard)"]
    assert json.loads((tmp_path / "pending_summaries.json").read_text()) == []


def test_phenom_pages_and_dates():
    def page(ids):
        return json.dumps({"refineSearch": {"data": {"jobs": [dict(jobSeqNo=f"R{i}", title=f"Treasury Analyst {i}", location="New York, NY",
                                                                     postedDate="2026-10-02T00:00:00.000+0000") for i in ids]}}}).encode()

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    answers = iter([b"<html></html>", page(range(50)), page(range(50, 53))])

    class Opener:
        def open(self, req, timeout=None):
            return Resp(next(answers))
    with mock.patch.object(js.urllib.request, "build_opener", lambda *a: Opener()):
        jobs, complete = js.phenom(dict(host="h.example", ref_num="R", page_id="page1"))
    assert complete and len(jobs) == 53
    assert jobs[0]["posted"] == "2026-10-02" and jobs[0]["url"] == "https://h.example/us/en/job/R0/Treasury-Analyst-0"
