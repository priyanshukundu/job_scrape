import json

import job_scout as j

FILTERS = json.load(open("data/config.json", encoding="utf-8"))["filters"]


def _ok(title):
    return j.matches({"title": title, "location": "New York, NY"}, FILTERS)


def test_spring_2027_internships_pass():
    assert _ok("Finance Intern, Spring 2027")
    assert _ok("Treasury Intern - Spring 2027")
    assert _ok("Spring 2027 Analyst Intern")


def test_other_internships_still_excluded():
    assert not _ok("Treasury Intern - Summer 2027")
    assert not _ok("Finance Intern, Fall 2026")


def test_spring_2027_still_hits_other_excludes():
    assert not _ok("Marketing Intern Spring 2027")
