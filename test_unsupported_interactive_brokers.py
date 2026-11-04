"""Offline shape test for the Dayforce fetcher (Interactive Brokers): a fake opener stands in for the network."""
import json
from unittest import mock

import job_scout as js


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


def posting(i, **kw):
    base = {"jobPostingId": i, "jobTitle": f" Role {i} ", "jobDescription": "<p>Do <b>work</b></p>",
            "postingStartTimestampUTC": "2026-10-09T10:00:00+00:00",
            "postingLocations": [{"formattedAddress": "Greenwich, CT, USA"}, {"formattedAddress": "Chicago, IL, USA"}]}
    base.update(kw)
    return base


def test_dayforce_sends_csrf_pages_and_maps_fields():
    seen = []
    pages = iter([{"jobPostings": [posting(1), posting(2, hasVirtualLocation=True)], "maxCount": 3},
                  {"jobPostings": [posting(3, postingLocations=[])], "maxCount": 3}])

    class Opener:
        def open(self, req, timeout=None):
            seen.append((req.full_url, req.get_header("X-csrf-token"), req.data))
            if req.full_url.endswith("/api/auth/csrf"):
                return Resp({"csrfToken": "tok123"})
            return Resp(next(pages))

    with mock.patch.object(js.urllib.request, "build_opener", lambda *a: Opener()):
        jobs, complete = js.dayforce(dict(namespace="ibgllc", board="CANDIDATEPORTAL"))
    assert complete and [j["id"] for j in jobs] == ["1", "2", "3"]
    assert jobs[0]["title"] == "Role 1" and jobs[0]["location"] == "Greenwich, CT, USA | Chicago, IL, USA"
    assert jobs[1]["location"].endswith("Remote") and jobs[2]["location"] == ""
    assert jobs[0]["posted"] == "2026-10-09" and jobs[0]["desc"] == "Do work"
    assert jobs[0]["url"] == "https://jobs.dayforcehcm.com/en-US/ibgllc/CANDIDATEPORTAL/jobs/1"
    assert all(tok == "tok123" for url, tok, _ in seen if "/jobposting/search" in url)
    assert json.loads(seen[2][2])["paginationStart"] == 2
