import time
import urllib.error
from unittest import mock

import job_scout as js


def _companies(n):
    return [dict(name=f"C{i:03d}", ats="greenhouse", slug=f"s{i}") for i in range(n)]


def test_shards_cover_every_company_exactly_once():
    cos = _companies(50)
    times = {c["name"]: float(1 + (i * 7) % 90) for i, c in enumerate(cos)}
    got = [c["name"] for i in range(6) for c in js.shard_companies(cos, i, 6, times)]
    assert sorted(got) == sorted(c["name"] for c in cos)


def test_shard_split_is_deterministic():
    cos = _companies(40)
    times = {c["name"]: float(5 + i) for i, c in enumerate(cos)}
    assert js.shard_companies(cos, 2, 6, times) == js.shard_companies(list(reversed(cos)), 2, 6, times)


def test_one_huge_company_does_not_drag_others_onto_its_shard():
    cos = _companies(30)
    times = {c["name"]: 10.0 for c in cos}
    times["C000"] = 700.0
    alone = js.shard_companies(cos, 0, 6, times)
    assert [c["name"] for c in alone][0] == "C000"
    assert len(alone) <= 2  # a big board's shard takes little else, since the wall time is its own


class _Resp:
    def __init__(self, code):
        self.code = code


def _workday_cfg():
    return dict(name="W", ats="workday", host="h.example.com", tenant="t", site="S", page_workers=2, term_workers=1)


def test_http_maps_status_to_httperror_and_reuses_connection(monkeypatch):
    calls = []

    class FakeResp:
        def __init__(self, status, body=b"{}"):
            self.status, self.reason, self.msg, self._b, self.will_close = status, "x", {"Retry-After": "3"}, body, False

        def read(self):
            return self._b

    class FakeConn:
        made = []

        def __init__(self, netloc, timeout=None):
            self.sock = None
            FakeConn.made.append(netloc)

        def request(self, method, path, body=None, headers=None):
            calls.append((method, path))

        def getresponse(self):
            return FakeResp(429 if len(calls) == 2 else 200, b'{"ok": 1}')

        def close(self):
            pass

    monkeypatch.setattr(js, "HTTPSConnection", FakeConn)
    monkeypatch.setattr(js, "_PROXIED", False)
    js._CONN_LOCAL.__dict__.pop("conns", None)
    monkeypatch.setattr(js, "RETRY_429_MAX", 0)
    assert js.http("https://a.example/x?y=1") == {"ok": 1}
    try:
        js.http("https://a.example/z", {"q": 1})
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as e:
        assert e.code == 429 and e.headers.get("Retry-After") == "3"
    assert FakeConn.made == ["a.example"]  # second request reused the first connection
    assert calls[0] == ("GET", "/x?y=1") and calls[1][0] == "POST"


def test_stale_keepalive_connection_is_retried_once_on_a_fresh_one(monkeypatch):
    from http.client import RemoteDisconnected

    class FakeResp:
        status, reason, msg, will_close = 200, "OK", {}, False

        def read(self):
            return b'{"v": 2}'

    class FakeConn:
        n = 0

        def __init__(self, netloc, timeout=None):
            FakeConn.n += 1
            self.id, self.sock = FakeConn.n, None

        def request(self, *a, **k):
            if self.id == 1 and getattr(FakeConn, "used", False):
                raise RemoteDisconnected("idle close")
            FakeConn.used = True

        def getresponse(self):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(js, "HTTPSConnection", FakeConn)
    monkeypatch.setattr(js, "_PROXIED", False)
    js._CONN_LOCAL.__dict__.pop("conns", None)
    assert js.http("https://b.example/1") == {"v": 2}
    assert js.http("https://b.example/2") == {"v": 2}  # first conn is stale now: transparently replaced
    assert FakeConn.n == 2


def _wd_http(lost_offsets, calls):
    """Fake Workday: 100 rows in pages of 20; the first request for each offset in lost_offsets answers 429."""
    seen = set()

    def fake(url, data=None, *a, **k):
        off = data.get("offset", 0)
        calls.append(off)
        if off in lost_offsets and off not in seen:
            seen.add(off)
            raise urllib.error.HTTPError(url, 429, "x", {}, None)
        n = max(0, min(data.get("limit", 20), 100 - off))
        return {"total": 100, "jobPostings": [dict(title=f"T{off + i}", externalPath=f"/job/{off + i}",
                                                   locationsText="NY", postedOn="Posted Today") for i in range(n)]}
    return fake


def test_lost_page_is_retried_alone_and_read_completes(monkeypatch):
    monkeypatch.setattr(js, "_FETCH_DEADLINE", [None])
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    calls = []
    with mock.patch.object(js, "http", _wd_http({40}, calls)):
        jobs, complete = js.workday(dict(_workday_cfg(), page_workers=1))
    assert complete and len(jobs) == 100
    assert calls.count(40) == 2 and calls.count(20) == 1  # only the lost page was requested again


def test_lost_page_retry_stops_at_the_shard_deadline(monkeypatch):
    monkeypatch.setattr(js, "_FETCH_DEADLINE", [time.time() + 10])  # inside the 30s safety margin: no retry pass
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    monkeypatch.setattr(js, "_SEQ_RETRY_BUDGET", [4])
    calls = []
    with mock.patch.object(js, "http", _wd_http({40}, calls)):
        jobs, complete = js.workday(dict(_workday_cfg(), page_workers=1))
    assert not complete  # the lost page stays lost, so nothing may be closed on this read
    assert calls.count(40) == 1


def test_retry_failure_leaves_page_lost_instead_of_raising(monkeypatch):
    monkeypatch.setattr(js, "_FETCH_DEADLINE", [None])
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    seen = []

    def fake(url, data=None, *a, **k):
        off = data.get("offset", 0)
        if off == 40:
            seen.append(off)
            raise urllib.error.HTTPError(url, 429 if len(seen) == 1 else 503, "x", {}, None)
        n = max(0, min(data.get("limit", 20), 100 - off))
        return {"total": 100, "jobPostings": [dict(title=f"T{off + i}", externalPath=f"/job/{off + i}",
                                                   locationsText="NY", postedOn="Posted Today") for i in range(n)]}
    with mock.patch.object(js, "http", fake):
        jobs, complete = js.workday(dict(_workday_cfg(), page_workers=1))
    assert not complete and len(jobs) == 80
