"""The per-run alert cap is global across companies: no run sends more than ALERT_CAP_PER_RUN individual alerts."""
import argparse
import json

import job_scout as js


def _job(i):
    return dict(id=str(i), title=f"Treasury Analyst {i}", location="New York, NY", url=f"https://x/{i}", posted="2026-10-01")


def test_no_run_sends_more_than_cap_individual_alerts_across_companies(tmp_path, monkeypatch):
    names = ["A", "B", "C", "D"]
    old = [_job(i) for i in range(30)]
    new = [_job(100 + i) for i in range(10)]  # 10 fresh of 40 hits: under the flood guard, so each company alerts individually
    (tmp_path / "config.json").write_text(json.dumps(dict(
        notify={}, filters=dict(title_include=["treasury"]), keywords=[],
        companies=[dict(name=n, ats="fake") for n in names])))
    (tmp_path / "state.json").write_text(json.dumps({n: [str(i) for i in range(30)] for n in names}))
    monkeypatch.setattr(js, "TITLE_TERMS", list(js.TITLE_TERMS))  # run() reloads the global list; restore it so other tests see the original
    monkeypatch.setattr(js, "HOME", tmp_path)
    monkeypatch.setattr(js, "fetch_all", lambda companies: ({n: (old + new, True, None) for n in names}, {}))
    monkeypatch.setattr(js, "describe", lambda company, job: ("", "", ""))
    monkeypatch.setattr(js, "_reverify_recent_posted", lambda q, c: None)
    sent = []
    monkeypatch.setattr(js, "notify", lambda cfg, entry, dry: sent.append(entry) or True)
    js.run(argparse.Namespace(from_fetched=None, dry_run=False, check=False))

    individual = [e for e in sent if e["company"] != "Summary"]
    summaries = [e for e in sent if e["company"] == "Summary"]
    assert js.ALERT_CAP_PER_RUN == 30
    assert len(individual) == js.ALERT_CAP_PER_RUN  # 40 fresh in total, capped globally at 30
    assert len(summaries) == 1 and "10 more new roles" in summaries[0]["role"]
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 40  # the overflow is still queued, just silently
