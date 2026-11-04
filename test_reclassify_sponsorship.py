"""Offline regression tests for sponsorship re-fetches from queue rows."""
import json
import sys

import pytest

import job_scout
import reclassify_sponsorship as backfill


@pytest.mark.parametrize("dry_run", [False, True])
def test_refetch_uses_queue_link(tmp_path, monkeypatch, capsys, dry_run):
    company = {"name": "Acme", "ats": "avature", "host": "acme.example"}
    entry = dict(id="Acme:42", company="Acme", role="Analyst", state="new",
                 link="https://acme.example/careers/42", site="external",
                 sponsorship_status="Unclear", sponsorship_evidence="")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps([entry]), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"companies": [company]}), encoding="utf-8")
    calls = []

    def fake_http(url, **kwargs):
        calls.append(url)
        return b"<html><p>U.S. citizenship is required.</p></html>"

    monkeypatch.setattr(job_scout, "http", fake_http)
    monkeypatch.setattr(backfill, "HOME", tmp_path)
    monkeypatch.setattr(sys, "argv", ["reclassify_sponsorship.py"] + (["--dry-run"] if dry_run else []))
    backfill.main()

    assert calls == [entry["link"]]
    assert "0 fetch errors" in capsys.readouterr().out
    saved = json.loads(queue_path.read_text(encoding="utf-8"))[0]
    if dry_run:
        assert saved == entry
    else:
        assert saved["sponsorship_status"] == "Blocked"
        assert saved["state"] == "auto_blocked"
        assert saved["sponsorship_evidence"]
        for key in ("id", "company", "role", "link", "site"):
            assert saved[key] == entry[key]
