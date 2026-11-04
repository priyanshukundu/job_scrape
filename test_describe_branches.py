"""Offline tests for the describe() branches that queue rows (which store no description) depend on: canned responses stand in for http()."""
import http.cookiejar
import io
import json
from unittest import mock

import job_scout as js
from test_fetchers import fake_http

LONG = "Responsibilities include building and supporting the trading platform. " * 3


def test_lever_single_posting():
    posting = {"descriptionPlain": "About the role.\n\xa0\nBuild things.", "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
               "additionalPlain": "Equal opportunity."}
    with mock.patch.object(js, "http", fake_http({"/v0/postings/gmo/abc-1": posting})):
        text, posted, src = js.describe({"ats": "lever", "slug": "gmo"}, {"id": "abc-1", "url": "https://jobs.lever.co/gmo/abc-1"})
    assert text == "About the role. Build things. Requirements Python Equal opportunity."
    assert (posted, src) == ("", "")


def test_lever_eu_region_host():
    seen = []

    def _http(url, **kw):
        seen.append(url)
        return {"descriptionPlain": "x"}
    with mock.patch.object(js, "http", _http):
        js.describe({"ats": "lever", "slug": "s", "region": "eu"}, {"id": "1"})
    assert seen == ["https://api.eu.lever.co/v0/postings/s/1"]


def test_deshaw_reads_next_data_not_jsonld():
    data = {"props": {"pageProps": {"jobData": {"jobDescription": {
        "websiteDescription": "<p>Intro paragraph.</p>", "responsibilitiesHtml": "<ul><li>Do A</li><li>Do B</li></ul>",
        "peopleWeAreLookingForStr": "Someone sharp."}}}}}
    page = f'<script type="application/ld+json">{{"description": "Intro only"}}</script><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    with mock.patch.object(js, "http", fake_http({"deshaw.com/careers/x-1": page})):
        text, _, _ = js.describe({"ats": "deshaw"}, {"id": "1", "url": "https://www.deshaw.com/careers/x-1"})
    assert text == "Intro paragraph. Do A Do B Someone sharp."


def test_deshaw_without_next_data_is_empty():
    with mock.patch.object(js, "http", fake_http({"deshaw.com": "<html>nothing</html>"})):
        assert js.describe({"ats": "deshaw"}, {"id": "1", "url": "https://www.deshaw.com/careers/x-1"}) == ("", "", "")


def test_hrmdirect_page_text_without_scripts():
    page = ("<html><head><style>.a{color:red}</style><script>var x=1;</script></head><body><nav>Menu</nav>"
            f"<h1>Analyst</h1><p>Location: New York, NY Type of Hire: Experienced</p><p>{LONG}</p></body></html>").encode("cp1252")
    with mock.patch.object(js, "http", fake_http({"job-opening.php": page})):
        text, _, _ = js.describe({"ats": "hrmdirect", "host": "opco.hrmdirect.com"},
                                 {"id": "7", "url": "https://opco.hrmdirect.com/employment/job-opening.php?req=7&&nohd"})
    assert text.startswith("Analyst Location: New York, NY") and "trading platform" in text
    assert "Menu" not in text and "color:red" not in text and "var x" not in text


def test_goldman_role_by_url_id():
    calls = []

    def gql(body):
        calls.append(body["variables"])
        return {"data": {"role": {"descriptionHtml": "<p>About the <b>Role</b></p><li>Lead models</li>"}}}
    with mock.patch.object(js, "http", fake_http({"api-higher.gs.com": gql})):
        text, _, _ = js.describe({"ats": "goldman"}, {"id": "179299_GS_MID_CAREER", "url": "https://higher.gs.com/roles/179299"})
        none = js.describe({"ats": "goldman"}, {"id": "5_X", "url": ""})
    assert text == "About the Role Lead models"
    assert calls[0] == {"id": "179299"} and calls[1] == {"id": "5"}  # url-less rows fall back to the id prefix
    assert none[0] == "About the Role Lead models"


def test_goldman_missing_role_is_empty():
    with mock.patch.object(js, "http", fake_http({"api-higher.gs.com": {"data": {"role": None}}})):
        assert js.describe({"ats": "goldman"}, {"id": "1_X", "url": "https://higher.gs.com/roles/1"})[0] == ""


class _UkgResp:
    def __init__(self, body):
        self.body = body.encode()

    def read(self):
        return self.body


def test_ukg_embedded_opportunity_description():
    obj = json.dumps({"Id": "x", "Description": "<div>Description</div><ul><li>Lead a branch</li></ul>"})
    page = f"<script>CandidateOpportunityDetail({obj});</script>"
    with mock.patch.object(js.urllib.request, "urlopen", lambda req, timeout=None: _UkgResp(page)):
        text, _, _ = js.describe({"ats": "ukg"}, {"id": "x", "url": "https://t.rec.pro.ukg.net/C/JobBoard/u/OpportunityDetail?opportunityId=x"})
    assert text == "Description Lead a branch"


def test_ukg_page_without_description_is_empty():
    with mock.patch.object(js.urllib.request, "urlopen", lambda req, timeout=None: _UkgResp("<html>nothing</html>")):
        assert js.describe({"ats": "ukg"}, {"id": "x", "url": "https://t.rec.pro.ukg.net/C/JobBoard/u/OpportunityDetail?opportunityId=x"})[0] == ""


def test_bpce_slug_to_post_then_main_text():
    routes = {"wp/v2/job?slug=risk-lead-2": [{"id": 20545}],
              "_uid=job-20545-x": {"content": {"main": {"text": "<h2>Job Description</h2><p>Lead <b>risk</b>.</p>"}}}}
    with mock.patch.object(js, "http", fake_http(routes)):
        text, _, _ = js.describe({"ats": "bpce", "host": "recrutement.natixis.com"},
                                 {"id": "21963", "url": "https://recrutement.natixis.com/en/job/risk-lead-2"})
    assert text == "Job Description Lead risk ."


def test_bpce_unknown_slug_is_empty():
    with mock.patch.object(js, "http", fake_http({"wp/v2/job": []})):
        assert js.describe({"ats": "bpce", "host": "h"}, {"id": "1", "url": "https://h/en/job/gone"}) == ("", "", "")


class _Resp(io.BytesIO):
    pass


class _FakeOpener:
    """Stands in for the cookie-jar opener: the search page sets the session cookie, JobDetails answers JSON."""
    def __init__(self, jar, detail):
        self.jar, self.detail, self.posts = jar, detail, []

    def open(self, req, timeout=0):
        if req.data is None:
            for name, val in (("tg_session_1_2", "TOK"),):
                self.jar.set_cookie(http.cookiejar.Cookie(
                    0, name, val, None, False, "h", False, False, "/", True, False, None, False, None, None, {}))
            return _Resp(b'<input name="__RequestVerificationToken" type="hidden" value="RFTVAL" />')
        self.posts.append((req.full_url, json.loads(req.data), req.get_header("Rft")))
        return _Resp(json.dumps(self.detail).encode())


def test_brassring_job_details_textareas():
    qs = [{"QuestionName": "", "AnswerValue": "Associate", "QuestionType": "text"},
          {"QuestionName": "Key responsibilities", "AnswerValue": "Build <b>things</b>&amp;stuff.", "QuestionType": "textarea"},
          {"QuestionName": "Your skills", "AnswerValue": "Python<br>SQL", "QuestionType": "textarea"},
          {"QuestionName": "City", "AnswerValue": "Weehawken", "QuestionType": "multi-select"}]
    detail = {"ServiceResponse": {"Jobdetails": {"JobDetailQuestions": qs}}}
    holder = {}

    def build_opener(proc):
        holder["op"] = _FakeOpener(proc.cookiejar, detail)
        return holder["op"]
    with mock.patch.object(js.urllib.request, "build_opener", build_opener):
        text, _, _ = js.describe({"ats": "brassring", "host": "h", "partnerid": "1", "siteid": "2"}, {"id": "352008", "url": "x"})
    assert text == "Key responsibilities Build things &stuff. Your skills Python SQL"
    url, body, rft = holder["op"].posts[0]
    assert url.endswith("/TgNewUI/Search/Ajax/JobDetails") and rft == "RFTVAL"
    assert body["jobid"] == "352008" and body["encryptedSessionValue"] == "TOK"
