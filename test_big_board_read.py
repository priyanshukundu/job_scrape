"""Offline tests (mocked js.http): a big board whose reported total is clamped is read whole with the empty search
instead of one search per title term."""
import urllib.error
import urllib.parse
from unittest import mock

import job_scout as js

TERMS = [f"term{i}" for i in range(5)]


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def _row(i):
    return {"externalPath": f"/job/NYC/T{i}_R{i}", "title": f"Title {i}", "locationsText": "NYC",
            "postedOn": "Posted Today"}


def _board(n, mode="open", fam=None, fail_offsets=(), log=None):
    """Fake Workday board of n rows. mode 'open': total clamped at 2000 but every offset answers (PwC/Lowe's style
    once the total is real, here deliberately clamped); 'capped': offsets >= 2000 repeat page 0 (Citi/Booz Allen);
    'real': the total is the true size. `fam` maps a job index to a jobFamily facet id (facet slices)."""
    def fake(url, data=None, **kw):
        off, lim, term, ap = data["offset"], data["limit"], data["searchText"], data["appliedFacets"]
        if log is not None:
            log.append(dict(offset=off, term=term, applied=ap, limit=lim))
        if off in fail_offsets:
            raise _http_error(429)
        idx = list(range(n))
        if ap.get("family"):
            idx = [i for i in idx if fam[i] == ap["family"][0]]
        if term:  # a term search matches every 7th title, enough for the fallback path
            idx = [i for i in idx if i % 7 == 0]
        total = len(idx) if mode == "real" else min(len(idx), 2000)
        # past the end (or past the cap) the real endpoint answers with page 0 again instead of an empty page
        eff = 0 if (mode == "capped" and off >= 2000) or off >= len(idx) else off
        rows = [_row(i) for i in idx[eff:eff + lim]]
        body = {"total": total, "jobPostings": rows}
        if fam is not None:
            ids = sorted(set(fam[i] for i in range(n)))
            body["facets"] = [{"facetParameter": "family", "values": [
                {"descriptor": f, "id": f, "count": sum(1 for i in range(n) if fam[i] == f)} for f in ids]}]
        return body
    return fake


def _co(**extra):
    return dict(name="Big", ats="workday", host="h.wd1.myworkdayjobs.com", tenant="t", site="S", search=TERMS,
                max_per_term=100, **extra)


def test_clamped_total_board_is_read_whole_with_empty_search():
    log = []
    with mock.patch.object(js, "http", _board(3140, "open", log=log)):
        jobs, complete = js.workday(_co())
    assert len(jobs) == 3140 and complete is True
    assert {x["term"] for x in log} == {""}  # no term searches at all
    assert len({j["id"] for j in jobs}) == 3140


def test_probe_finds_true_size():
    for n in (2020, 2001, 2019, 2020, 2021, 3140, 4000, 12644):
        calls = []

        def rows(off, n=n):
            calls.append(off)
            if off >= n:
                return [_row(i) for i in range(20)]  # past the end Workday repeats page 0 rather than answer empty
            return [_row(i) for i in range(off, min(off + 20, n))]
        assert js._probe_true_size(rows, 20) == n, n
        assert len(calls) < 25, (n, len(calls))  # a handful of requests, not a page walk


def test_probe_flags_ignored_offsets_and_oversize_boards():
    page0 = [_row(i) for i in range(20)]
    assert js._probe_true_size(lambda off: page0, 20) == -1
    big = lambda off: [_row(i) for i in range(off, off + 20)]  # noqa: E731  never ends
    assert js._probe_true_size(big, 20, max_probe=8000) is None


def test_lost_429_page_makes_whole_read_incomplete():
    with mock.patch.object(js, "http", _board(3140, "open", fail_offsets=(2500,))):
        jobs, complete = js.workday(_co())
    assert complete is False
    assert len(jobs) == 3140 - 20  # the other pages are kept


def test_duplicate_pages_past_clamp_fall_back_safely():
    log = []
    with mock.patch.object(js, "http", _board(3140, "capped", log=log)):
        jobs, complete = js.workday(_co())
    assert any(x["term"] for x in log)  # no facets to slice by: back to the term search
    assert len(jobs) > 0


def test_capped_board_with_facets_is_read_in_slices_and_completes():
    fam = {i: f"f{i % 4}" for i in range(3200)}  # four slices of exactly 800 rows: a page boundary at the end
    log = []
    with mock.patch.object(js, "http", _board(3200, "capped", fam=fam, log=log)):
        jobs, complete = js.workday(_co())
    assert len(jobs) == 3200 and complete is True
    assert not any(x["term"] for x in log)


def test_capped_board_with_unfaceted_postings_is_incomplete():
    fam = {i: f"f{i % 4}" for i in range(3140)}
    inner = _board(3140, "capped", fam=fam)

    def fake(url, data=None, **kw):
        d = inner(url, data=data, **kw)
        for f in d.get("facets", []):  # facet counts claim more postings than the slices return
            f["values"][0]["count"] += 400
        return d
    with mock.patch.object(js, "http", fake):
        jobs, complete = js.workday(_co())
    assert complete is False


def test_board_over_board_read_max_falls_back_to_term_search():
    log = []
    with mock.patch.object(js, "http", _board(3140, "open", log=log)):
        jobs, complete = js.workday(_co(board_read_max=3000))
    assert {x["term"] for x in log} >= set(TERMS)
    assert len(jobs) < 3140


def test_small_board_path_unchanged():
    log = []
    with mock.patch.object(js, "http", _board(1500, "real", log=log)):
        jobs, complete = js.workday(_co())
    assert len(jobs) == 1500 and complete is True
    assert {x["term"] for x in log} == {""}


def _oracle_http(n, fail_offset=None, log=None):
    def fake(url, **kw):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["finder"][0]
        parts = dict(p.split("=", 1) for p in q.split(";", 1)[1].split(","))
        off, lim = int(parts["offset"]), int(parts["limit"])
        if log is not None:
            log.append(parts)
        if fail_offset is not None and off == fail_offset:
            raise _http_error(429)
        keyword = parts.get("keyword")
        ids = [i for i in range(n) if not keyword or i % 7 == 0]
        rows = [{"Id": str(1000 + i), "Title": f"T{i}", "PrimaryLocation": "NY", "PostedDate": "2026-01-01"}
                for i in ids[off:off + lim]]
        return {"items": [{"TotalJobsCount": len(ids), "requisitionList": rows}]}
    return fake


def _oc(**extra):
    return dict(name="Orc", ats="oracle", host="o.fa.oraclecloud.com", site="CX_1", search=TERMS, max_per_term=100,
                **extra)


def test_oracle_big_board_is_read_whole_without_keywords():
    log = []
    with mock.patch.object(js, "http", _oracle_http(7322, log=log)):
        jobs, complete = js.oracle(_oc())
    assert len(jobs) == 7322 and complete is True
    assert not any("keyword" in p for p in log)
    assert len(log) < 160  # ~150 pages of 50, plus the size probe


def test_oracle_lost_page_is_incomplete_and_over_max_uses_keywords():
    with mock.patch.object(js, "http", _oracle_http(7322, fail_offset=3000)):
        jobs, complete = js.oracle(_oc())
    assert complete is False and len(jobs) == 7322 - 50
    log = []
    with mock.patch.object(js, "http", _oracle_http(7322, log=log)):
        js.oracle(_oc(board_read_max=5000))
    assert any("keyword" in p for p in log)


def test_short_parallel_read_is_retried_sequentially_and_completes():
    inner = _board(1500, "real")
    state = {"calls": 0}

    def flaky(url, data=None, **kw):
        state["calls"] += 1
        if data["offset"] == 400 and not state.get("failed"):
            state["failed"] = True  # one page lost to a 429 on the parallel pass only
            raise _http_error(429)
        return inner(url, data=data, **kw)
    js._SEQ_RETRY_BUDGET[0] = 4
    with mock.patch.object(js, "http", flaky):
        jobs, complete = js.workday(_co())
    assert complete is True
    assert len({j["id"] for j in jobs}) == 1500


def test_sequential_retry_budget_is_respected():
    js._SEQ_RETRY_BUDGET[0] = 0
    with mock.patch.object(js, "http", _board(3140, "open", fail_offsets=(2500,))):
        jobs, complete = js.workday(_co())
    assert complete is False
    js._SEQ_RETRY_BUDGET[0] = 4
