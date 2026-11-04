"""Offline shape tests for the Balyasny Aura fetcher: canned Salesforce responses stand in for http()."""
import json
from unittest import mock

import job_scout as js

PAGE = ('<script src="/s/sfsites/l/%7B%22mode%22%3A%22PROD%22%2C%22fwuid%22%3A%22FW123%22%2C%22loaded%22%3A%7B%22APPLICATION%40markup'
        '%3A%2F%2Fsiteforce%3AcommunityApp%22%3A%22APP456%22%7D%7D/app.js"></script>')

ROW = {"Id": "a0D1", "Name": "REQ - Paralegal", "Publish_Title__c": "Paralegal", "Requisition_Number__c": "REQ8670",
       "Job_Req_Title_in_URL__c": "Paralegal", "Posted_Ago__c": "Posted 4 Days Ago",
       "Job_Requisition_Positions__r": [{"Location__r": {"External_Name__c": "New York"},
                                         "Job_Requisition_Position_Locations__r": [{"Location__r": {"External_Name__c": "Chicago"}},
                                                                                  {"Location__r": {"External_Name__c": "New York"}}]}]}


def fake_http(sent, rv):
    def _http(url, data=None, headers=None, timeout=20, raw=False):
        if url.endswith("/s/"):
            return PAGE.encode()
        sent.append(data.decode())
        return json.dumps({"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": rv}}]}).encode()
    return _http


def test_balyasny_list_maps_rows_and_sends_context():
    sent = []
    with mock.patch.object(js, "http", fake_http(sent, [ROW, {"Id": "a0D2", "Publish_Title__c": ""}])):
        jobs, complete = js.balyasny({})
    assert complete and len(jobs) == 1
    j = jobs[0]
    assert j["id"] == "a0D1" and j["title"] == "Paralegal" and j["location"] == "New York | Chicago"
    assert j["url"] == "https://bambusdev.my.site.com/s/details?jobReq=Paralegal_REQ8670"
    assert j["posted"] != ""
    assert "FW123" in sent[0] and "APP456" in sent[0] and "searchJobRequisitions" in sent[0]


def test_balyasny_describe_uses_record_id():
    sent = []
    with mock.patch.object(js, "http", fake_http(sent, {"Description__c": "<p>Do <b>legal</b> work</p>"})):
        text, posted, source = js.describe({"ats": "balyasny"}, {"id": "a0D1", "url": "u"})
    assert text == "Do legal work" and (posted, source) == ("", "")
    assert "getDescription" in sent[0] and "a0D1" in sent[0]


def test_balyasny_failed_call_raises():
    def bad(url, data=None, headers=None, timeout=20, raw=False):
        if url.endswith("/s/"):
            return PAGE.encode()
        return json.dumps({"actions": [{"state": "ERROR", "error": ["x"]}]}).encode()
    with mock.patch.object(js, "http", bad):
        try:
            js.balyasny({})
        except ValueError:
            return
    raise AssertionError("expected ValueError")
