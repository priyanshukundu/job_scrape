"""Offline tests for card-location fallbacks added for Bloomberg (Avature) and Lord Abbett / America First (iCIMS)."""
from unittest import mock

import job_scout as js


def fake_http(routes):
    def _http(url, data=None, headers=None, timeout=20, raw=False):
        for key, payload in routes.items():
            if key in url:
                return payload.encode() if raw else payload
        raise AssertionError(f"unexpected url {url}")
    return _http


def test_avature_reads_list_item_location_when_no_paragraph_spans():
    card = ('<article class="article article--result" id="a1"><h3><a class="link" href="https://b.example/careers/JobDetail/Quant-Analyst/45200">'
            ' Quant Analyst </a></h3><div><span class="list-item-location">New York, NY, United States</span></div></article>')
    with mock.patch.object(js, "http", fake_http({"SearchJobs": "<html>" + card + "</html>"})):
        jobs, complete = js.avature(dict(host="b.example", list_path="careers/SearchJobs"))
    assert complete and len(jobs) == 1
    assert jobs[0]["id"] == "45200" and jobs[0]["location"] == "New York, NY, United States"


def test_icims_joins_split_city_and_state_labels():
    card = ('<div class="row"><div class="col-xs-6 header left"><span class="sr-only field-label">Location : City</span> <span > Roy</span></div>'
            '<div class="col-xs-12 title"><a href="https://p.example/jobs/21151/sr-product-manager/job?in_iframe=1" class="iCIMS_Anchor">'
            '<span class="sr-only field-label">Job Title</span><h3 > Sr Product Manager</h3></a></div>'
            '<dl><dt><span class="sr-only field-label">Location : State/Province (Full Name)</span></dt><dd><span > Utah</span></dd></dl></div></li>')
    page = '<ul><li><div class="iCIMS_JobCardItem"' + card
    with mock.patch.object(js, "http", fake_http({"pr=0": page, "pr=1": ""})):
        jobs, complete = js.icims(dict(hosts=["p.example"]))
    assert complete and len(jobs) == 1
    assert jobs[0]["location"] == "Roy, Utah" and jobs[0]["id"] == "21151"


def test_icims_wafd_header_label_and_berkley_primary_location():
    def page(header, extra):
        card = (f'<div class="row"><div class="col-xs-6 header left">{header}</div>'
                '<div class="col-xs-12 title"><a href="https://p.example/jobs/5181/branch-manager/job?in_iframe=1" class="iCIMS_Anchor">'
                '<span class="sr-only field-label">Title</span><h3 > Branch Manager</h3></a></div>'
                f'{extra}</div></li>')
        return '<ul><li><div class="iCIMS_JobCardItem"' + card
    wafd = page('<span class="sr-only field-label">Location : Location</span> <span > US-WA-Bremerton</span>', "")
    berkley = page('<span class="sr-only field-label">Company</span> <span > Berkley One</span>',
                   '<dl><div><dt><span class="sr-only field-label">Primary Location</span></dt><dd class="d"><span > US-DE-Wilmington</span></dd></div></dl>')
    for html, want in ((wafd, "Bremerton, WA, US"), (berkley, "Wilmington, DE, US")):
        with mock.patch.object(js, "http", fake_http({"pr=0": html, "pr=1": ""})):
            jobs, _ = js.icims(dict(hosts=["p.example"]))
        assert jobs[0]["location"] == want
