"""Offline shape tests for the Tal.net fetcher (Evercore): canned Atom feed and posting page stand in for http()."""
from unittest import mock

import job_scout as js

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Experienced Hires</title>
  <entry><id>https://x.tal.net/vx/a/opp/3400-Vice-President-Benefits/en-GB</id>
    <link rel="alternate" href="https://x.tal.net/vx/a/opp/3400-Vice-President-Benefits/en-GB?instant=apply"/>
    <title>Vice President - Benefits</title><published>2026-10-02T21:18:40Z</published></entry>
  <entry><id>https://x.tal.net/vx/a/opp/3401-Analyst-Boston/en-GB</id><title>Analyst</title><published>2026-10-03T01:00:00Z</published></entry>
  <entry><id>https://x.tal.net/vx/a/no-opp-here</id><title>skipped</title></entry>
</feed>"""

PAGE = """<html><div class="form-group"><label><span class="hform_lbl_text"  >Region</span></label>
<div class="form-control-static" role="definition" data-field_value="749">

Americas
</div></div>
<div class="form-group"><label><span class="hform_lbl_text"  >Location</span></label>
<div class="form-control-static" role="definition" data-field_value="7">
New York
</div></div>
<div class="form-group"><label><span class="hform_lbl_text"  >Job description</span></label>
<div class="form-control-static"><p>Support the <b>benefits</b> team.</p></div></div></html>"""


def fake_http(url, data=None, headers=None, timeout=20, raw=False):
    if url.endswith("/feed"):
        return FEED.encode()
    if "3401" in url:
        raise OSError("boom")
    return PAGE.encode()


def test_tal_joins_feed_to_posting_page():
    with mock.patch.object(js, "http", fake_http):
        jobs, complete = js.tal(dict(host="x.tal.net", vacancies=[3]))
    assert not complete  # the 3401 page failed, so the list is flagged incomplete
    assert [j["id"] for j in jobs] == ["3400"]
    j = jobs[0]
    assert j["title"] == "Vice President - Benefits" and j["posted"] == "2026-10-02"
    assert j["location"] == "New York, Americas" and j["url"].endswith("/opp/3400-Vice-President-Benefits/en-GB")
    assert "Support the benefits team." in j["desc"]


def test_tal_complete_when_all_pages_read():
    def ok(url, data=None, headers=None, timeout=20, raw=False):
        return FEED.encode() if url.endswith("/feed") else PAGE.encode()
    with mock.patch.object(js, "http", ok):
        jobs, complete = js.tal(dict(host="x.tal.net"))
    assert complete and len(jobs) == 2
