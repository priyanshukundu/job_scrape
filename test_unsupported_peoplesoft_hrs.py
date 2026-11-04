"""Offline shape test for the peoplesoft_hrs fetcher (PeopleSoft Candidate Gateway, e.g. Federated Hermes)."""
from unittest import mock

import job_scout as js


class FakeResp:
    def __init__(self, payload):
        self.payload = payload.encode()

    def read(self):
        return self.payload


def form(sid, num):
    return f"<input name='ICStateNum' id='ICStateNum' value='{num}' /><input name='ICSID' id='ICSID' value='{sid}' />"


def row(i, jid, title, loc, posted):
    lab = lambda k, v: f"<DIV><span  class='ps-label'>{k}</span></DIV><span class='ps_box-value'   id='X${i}' >{v}</span>"
    return (f"<DIV id='win0divSCH_JOB_TITLE${i}'>{lab('Job Title', title)}{lab('Job ID', jid)}{lab('Location', loc)}"
            f"{lab('Posted Date', posted)}<span id='SCH_JOB_TITLE${i}'></span></DIV>")


class FakeOpener:
    def __init__(self):
        self.calls = []

    def open(self, req, timeout=None):
        body = req.data.decode() if req.data else ""
        self.calls.append(body)
        if not body:
            return FakeResp(form("S1", 1))
        if "HRS_SCH_WRK_FLU_HRS_SEARCH_BTN" in body:
            return FakeResp(form("S2", 3) + "<b>3</b> jobs found" + row(0, "11", "Analyst &amp; Co", "Pittsburgh", "10/08/2026"))
        assert "hdown" in body and "ICSID=S2" in body
        return FakeResp("<b>3</b> jobs found" + row(0, "11", "Analyst &amp; Co", "Pittsburgh", "10/08/2026")
                        + row(1, "12", "Quant", "New York City", "09/30/2026") + row(2, "13", "Trader", "Boston", "") + row(3, "13", "Dup", "Boston", ""))


def test_peoplesoft_hrs_search_then_show_all():
    op = FakeOpener()
    with mock.patch.object(js.urllib.request, "build_opener", return_value=op):
        jobs, complete = js.peoplesoft_hrs(dict(host="h.example"))
    assert complete and [j["id"] for j in jobs] == ["11", "12", "13"]
    assert jobs[0]["title"] == "Analyst & Co" and jobs[0]["location"] == "Pittsburgh" and jobs[0]["posted"] == "2026-10-08"
    assert jobs[1]["posted"] == "2026-09-30" and jobs[2]["posted"] == ""
    assert "JobOpeningId=11" in jobs[0]["url"] and jobs[0]["url"].startswith("https://h.example/psc/EXT/EMPLOYEE/HRMS/c/")


def test_peoplesoft_hrs_without_form_state_is_incomplete():
    class Blank(FakeOpener):
        def open(self, req, timeout=None):
            return FakeResp("<html>nothing</html>")

    with mock.patch.object(js.urllib.request, "build_opener", return_value=Blank()):
        assert js.peoplesoft_hrs(dict(host="h")) == ([], False)
