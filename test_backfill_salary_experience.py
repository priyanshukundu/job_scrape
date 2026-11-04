"""Offline tests for backfill_salary_experience.py (describe() mocked, all paths under tmp_path)."""
import json

import pytest

import backfill_salary_experience as bse
import job_scout

TEXT_RANGE = "The base salary range for this role is $120,000 - $150,000 per year. 3+ years of experience required."


def row(i, **kw):
    r = dict(id=f"Acme:{i}", company="Acme", role="Analyst", link=f"https://x/{i}", state="new", site=None,
             salary="Unclear", salary_evidence="", experience=None, experience_evidence="", sponsorship_status="Unclear")
    r.update(kw)
    return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"},
                                              {"name": "Lev", "ats": "lever", "slug": "lev"}]}))
    calls = []

    def fake_describe(c, job):
        calls.append(job["id"])
        if c["ats"] == "lever":
            return "", "", ""
        if job["id"] == "boom":
            raise RuntimeError("503")
        return TEXT_RANGE, "", ""
    monkeypatch.setattr(job_scout, "describe", fake_describe)
    monkeypatch.setattr(bse.time, "sleep", lambda s: None)

    def go(queue, **kw):
        q = tmp_path / "queue.json"
        q.write_text(json.dumps(queue))
        rep = bse.run(q, cfg, tmp_path / "out", rate=1000, **kw)
        return q, rep
    return tmp_path, go, calls


def patched(tmp_path):
    return {e["id"]: e for e in json.loads((tmp_path / "out" / "queue.patched.json").read_text())}


def test_overwrites_na_and_dry_run_leaves_queue_untouched(env):
    tmp_path, go, _ = env
    q, rep = go([row("1")])
    assert rep["salary_na_to_value"] == 1 and rep["experience_na_to_value"] == 1
    assert json.loads(q.read_text())[0]["salary"] == "Unclear"  # dry run: source queue unchanged
    p = patched(tmp_path)["Acme:1"]
    assert p["salary"] == "$120,000 - $150,000" and p["experience"] == 3
    assert p["salary_evidence"] and p["experience_evidence"]
    changes = [json.loads(x) for x in (tmp_path / "out" / "changes.jsonl").read_text().splitlines()]
    assert {c["field"] for c in changes} == {"salary", "experience"}


def test_apply_rewrites_queue(env):
    tmp_path, go, _ = env
    q, _ = go([row("1")], apply=True)
    assert json.loads(q.read_text())[0]["salary"] == "$120,000 - $150,000"


def test_upgrades_lower_bound_to_range(env):
    tmp_path, go, _ = env
    q, rep = go([row("1", salary="from $120,000", salary_evidence="starting at $120,000", experience=2)])
    assert rep["salary_lower_bound_to_range"] == 1
    assert patched(tmp_path)["Acme:1"]["salary"] == "$120,000 - $150,000"
    assert patched(tmp_path)["Acme:1"]["experience"] == 2  # real experience untouched


def test_inconsistent_range_does_not_replace_lower_bound(env):
    tmp_path, go, _ = env
    go([row("1", salary="from $300,000", salary_evidence="from $300,000", experience=2)])
    assert patched(tmp_path)["Acme:1"]["salary"] == "from $300,000"


def test_never_downgrades_real_values(env, monkeypatch):
    tmp_path, go, _ = env
    monkeypatch.setattr(job_scout, "describe", lambda c, j: ("no pay info here", "", ""))
    r = row("1", salary="$100,000 - $110,000", salary_evidence="x", experience=None)
    go([r, row("2", salary="from $90,000", salary_evidence="from $90,000", experience=4)])
    p = patched(tmp_path)
    assert p["Acme:1"]["salary"] == "$100,000 - $110,000"
    assert p["Acme:2"]["salary"] == "from $90,000" and p["Acme:2"]["experience"] == 4


def test_skips_closed_and_applied_dismissed_unless_all_states(env):
    tmp_path, go, calls = env
    qs = [row("1", closed_on="2026-10-01"), row("2", state="applied"), row("3", state="dismissed")]
    _, rep = go(qs)
    assert rep["rows_considered"] == 0 and calls == []
    _, rep = go(qs, all_states=True)
    assert rep["rows_considered"] == 2  # closed rows are always skipped


def test_no_describe_path_and_errors_are_not_fatal(env):
    tmp_path, go, _ = env
    qs = [row("1", company="Lev", id="Lev:1"), row("boom", id="Acme:boom"), row("4", company="Gone", id="Gone:4"), row("5")]
    _, rep = go(qs)
    assert rep["no_describe_path"] == 2 and rep["errors_this_run"] == 1 and rep["fetched_ok_total"] == 1
    assert rep["by_ats"]["lever"]["no_path"] == 1


def test_resumes_from_cache(env):
    tmp_path, go, calls = env
    go([row("1"), row("2")])
    assert sorted(calls) == ["1", "2"]
    calls.clear()
    _, rep = go([row("1"), row("2"), row("3")])
    assert calls == ["3"] and rep["salary_na_to_value"] == 3


def test_only_target_fields_change(env):
    tmp_path, go, _ = env
    r = row("1", posted="2026-09-01", sponsorship_evidence="keep me")
    go([r])
    p = patched(tmp_path)["Acme:1"]
    for k, v in r.items():
        if k not in ("salary", "salary_evidence", "experience", "experience_evidence"):
            assert p[k] == v


def test_stated_only_skips_title_guesses():
    rec = dict(status="ok", salary="Unclear", salary_evidence="", experience=6, experience_evidence="inferred from title: VP")
    r = row("g")
    assert [c["field"] for c in bse.apply_cached(r, rec)] == ["experience"]
    assert bse.apply_cached(r, rec, stated_only=True) == []
    rec["experience_evidence"] = "5+ years of experience required"
    assert [c["field"] for c in bse.apply_cached(r, rec, stated_only=True)] == ["experience"]
