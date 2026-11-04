"""Offline shape test for the taleo_cws fetcher (Taleo Business Edition Career Center v2, e.g. FHLBank Boston)."""
from unittest import mock

import job_scout as js

ROW = ('<div class="oracletaleocwsv2-accordion-head-info">\n<h4 class="oracletaleocwsv2-head-title">'
       '<a href="https://h.example/phh01/ats/careers/v2/viewRequisition?org=O&cws=38&rid={rid}" class="viewJobLink">{title}</a></h4>\n'
       '<div tabindex="0" >{loc}</div>\n</div>')


def test_taleo_cws_rows_and_dedupe():
    page = ('<html class="oracletaleocwsv2">' + ROW.format(rid=483, title="Sr Accountant &ndash; SEC &amp; Reporting", loc="Boston")
            + ROW.format(rid=474, title="Market Risk Analyst II", loc="Boston") + ROW.format(rid=474, title="Dup", loc="Boston") + "</html>")
    seen = []

    def fake(url, **kw):
        seen.append((url, kw))
        return page.encode()

    with mock.patch.object(js, "http", fake):
        jobs, complete = js.taleo_cws(dict(host="h.example", path="phh01", org="O", cws="38"))
    assert complete and [j["id"] for j in jobs] == ["483", "474"]
    assert jobs[0]["title"] == "Sr Accountant – SEC & Reporting" and jobs[0]["location"] == "Boston"
    assert jobs[0]["url"].endswith("rid=483") and "&amp;" not in jobs[0]["url"]
    assert seen[0][0] == "https://h.example/phh01/ats/careers/v2/searchResults?org=O&cws=38"


def test_taleo_cws_unrecognised_page_is_incomplete():
    with mock.patch.object(js, "http", lambda url, **kw: b"<html>blocked</html>"):
        assert js.taleo_cws(dict(host="h", path="p", org="O", cws="1")) == ([], False)
