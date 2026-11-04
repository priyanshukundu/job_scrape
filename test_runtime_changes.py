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


def test_sequential_reread_is_skipped_when_the_shard_budget_cannot_cover_it(monkeypatch):
    monkeypatch.setattr(js, "_FETCH_DEADLINE", [time.time() + 5])
    monkeypatch.setattr(js, "_SEQ_RETRY_BUDGET", [4])
    seq_calls = []
    real = js.workday

    def fake_post(*a, **k):
        raise urllib.error.HTTPError("u", 429, "x", {}, None)

    c = _workday_cfg()
    with mock.patch.object(js, "http", side_effect=fake_post):
        try:
            real(c)
        except Exception:
            pass
    assert js._SEQ_RETRY_BUDGET[0] == 4  # nothing consumed a sequential retry
    assert not seq_calls
