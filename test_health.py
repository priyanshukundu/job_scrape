"""Offline fixtures for observable health, unknown history and CLI output."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

import health


NOW = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)


@pytest.fixture
def fetched():
    return {
        "Good": {"jobs": [{"id": "1", "title": "Treasury", "posted": "2026-10-01", "desc": "Full text"}], "complete": True, "err": None},
        "Partial": {"jobs": [{"id": "2", "title": "Risk", "posted": None}], "complete": False, "err": None},
        "Empty": {"jobs": [], "complete": True, "err": None},
        "Failed": {"jobs": None, "complete": None, "err": "HTTP 500"},
    }


@pytest.fixture
def queue():
    return [
        {"id": "Good:1", "company": "Good", "state": "new", "alert_pending": True, "alert_attempts": 1, "posted": "2026-10-01"},
        {"id": "Partial:2", "company": "Partial", "state": "applied", "alert_pending": True},
        {"id": "Empty:3", "company": "Empty", "state": "new", "closed_on": "2026-10-03", "alert_pending": True},
        {"id": "Good:4", "company": "Good", "state": "new", "alert_pending": True, "alert_attempts": 3},
    ]


def test_summary_separates_fetch_outcomes_and_pending_alerts(fetched, queue):
    result = health.build_summary(queue, {"Good": ["1"], "Missing": []}, fetched, now=NOW,
                                  companies=["Good", "Partial", "Empty", "Failed", "Missing"])
    counts = result["counts"]
    assert counts["companies_expected"] == 5 and counts["companies_fetched_ok"] == 3
    assert counts["missing_fetches"] == 1 and counts["failed_fetches"] == 1
    assert counts["complete_boards"] == 2 and counts["verified_nonempty_boards"] == 1
    assert result["companies"]["incomplete"] == ["Partial"]
    assert result["companies"]["zero_results"] == ["Empty"]
    assert counts["pending_alerts"] == 4 and counts["eligible_pending_alerts"] == 1
    assert counts["inactive_pending_alerts"] == 3
    assert counts["missing_posted_dates"] == 1 and counts["queue_missing_posted_dates"] == 3


def test_legacy_state_and_sightings_do_not_invent_success_timestamps():
    queue = [{"company": "Old", "last_seen_live": "2026-10-03", "state": "new"}]
    result = health.build_summary(queue, {"Old": ["1"]}, {}, now=NOW)
    assert result["last_success"] == {"Old": None}
    assert result["counts"]["success_history_unknown"] == 1


def test_history_survives_failed_partial_empty_and_missing_fetches(fetched):
    names = ["Good", "Partial", "Empty", "Failed", "Missing", "Recent"]
    previous = {"last_success": {name: "2026-10-01T15:00:00Z" for name in names}}
    previous["last_success"]["Recent"] = "2026-10-03T15:00:00Z"
    result = health.build_summary([], {}, fetched, now=NOW, companies=names, previous=previous)
    assert result["last_success"]["Good"] == "2026-10-03T16:00:00Z"
    assert result["last_success"]["Partial"] == previous["last_success"]["Partial"]
    assert result["companies"]["stale_success"] == ["Empty", "Failed", "Missing", "Partial"]
    assert result["counts"]["no_success_lately"] == 4


def test_history_boundary_and_invalid_future_values():
    previous = {"last_success": {"Boundary": "2026-10-02T16:00:00Z", "Future": "2026-10-04T00:00:00Z", "Bad": "bad"}}
    result = health.build_summary([], {}, {}, now=NOW, companies=list(previous["last_success"]), previous=previous)
    assert result["companies"]["stale_success"] == []
    assert result["companies"]["unknown_success"] == ["Bad", "Future"]


def test_removed_companies_do_not_accumulate_in_history():
    result = health.build_summary([], {"Removed": ["1"]}, {}, now=NOW, companies=["Current"],
                                  previous={"last_success": {"Removed": "2026-10-03T15:00:00Z"}})
    assert result["last_success"] == {"Current": None}


def test_descriptions_distinguish_absent_bulk_fields_from_observed_failures():
    jobs = [{"id": "1", "title": "Empty", "desc": ""}, {"id": "2", "title": "Detail"},
            {"id": "3", "title": "Inline", "desc": "Text"}]
    log = "! A description for Empty: timeout\n! A description for Empty: timeout\n! A description for Detail: HTTP 500\n"
    result = health.build_summary([], {}, {"A": {"jobs": jobs, "complete": True, "err": None}}, now=NOW, scout_log=log)
    assert result["counts"]["missing_descriptions_observed"] == 2
    assert result["counts"]["detail_description_failures"] == 2
    assert result["counts"]["listing_description_unknown"] == 1


def test_queue_description_flags_preserve_legacy_unknowns():
    queue = [{"company": "A", "description_status": "failed"},
             {"company": "A", "description_status": "ok"}, {"company": "A"}]
    result = health.build_summary(queue, {}, {}, now=NOW)
    assert result["counts"]["queue_failed_descriptions"] == 1
    assert result["counts"]["queue_description_status_unknown"] == 1


def test_per_run_transitions_use_log_not_same_day_fields(queue):
    log = "done: 1 companies (0 errors), 0 new matches, 0 auto-blocked on sponsorship, 0 queued, 0 alerts, 2 closed, 7 reopened, 0 reposts, 0 pruned."
    result = health.build_summary(queue, {}, {}, now=NOW, scout_log=log)
    assert result["counts"]["closed_this_run"] == 2 and result["counts"]["reopened_this_run"] == 7
    assert result["scout_finished"]
    unknown = health.build_summary(queue, {}, {}, now=NOW)
    assert unknown["counts"]["closed_this_run"] is None
    assert unknown["counts"]["detail_description_failures"] is None


def test_duplicate_job_ids_count_once_and_inputs_stay_unchanged(fetched, queue):
    fetched["Good"]["jobs"].append(deepcopy(fetched["Good"]["jobs"][0]))
    state, previous = {"Good": ["1"]}, {"last_success": {"Good": "2026-10-01T00:00:00Z"}}
    before = deepcopy((queue, state, fetched, previous))
    result = health.build_summary(queue, state, fetched, now=NOW, previous=previous)
    assert result["counts"]["jobs_returned"] == 2
    assert (queue, state, fetched, previous) == before


@pytest.mark.parametrize("row", [None, {}, {"jobs": None, "complete": True, "err": None},
                                  {"jobs": [], "complete": "yes", "err": None},
                                  {"jobs": [None], "complete": True, "err": None}])
def test_malformed_fetch_is_not_reported_as_success(row):
    result = health.build_summary([], {}, {"A": row}, now=NOW)
    assert result["counts"]["failed_fetches"] == 1
    assert result["counts"]["companies_fetched_ok"] == 0
    assert result["input_errors"]


def test_conflicting_shards_never_choose_silent_success(fetched):
    merged, errors = health.merge_shards([("shard_1.json", {"Good": fetched["Good"]}),
                                          ("shard_0.json", {"Good": fetched["Empty"]}),
                                          ("shard_2.json", {"Good": fetched["Good"]})])
    assert errors and merged["Good"]["err"] == "conflicting shard results"
    identical, errors = health.merge_shards([("one", fetched), ("two", fetched)])
    assert identical == fetched and errors == []
    assert health.merge_shards([("bad", [])]) == ({}, ["bad: shard must be an object"])


def test_markdown_reports_unknowns_and_limits_details():
    result = health.build_summary([], {}, {}, now=NOW, companies=[f"Name{i}" for i in range(30)])
    text = health.render_markdown(result, limit=2)
    assert "Closed this run | unknown" in text and "and 28 more" in text
    assert "Legacy state IDs" in text and "not failed" in text


def test_cli_overwrites_small_output_appends_summary_and_never_changes_inputs(tmp_path, fetched):
    queue, state, config = tmp_path / "queue.json", tmp_path / "state.json", tmp_path / "config.json"
    shards = tmp_path / "shards"
    shards.mkdir()
    queue.write_text("[]", encoding="utf-8")
    state.write_text('{"Good": ["1"]}', encoding="utf-8")
    config.write_text('{"companies": [{"name":"Good"}, {"name":"Absent"}]}', encoding="utf-8")
    shard = shards / "shard_0.json"
    shard.write_text(json.dumps({"Good": fetched["Good"]}), encoding="utf-8")
    log = tmp_path / "scout.log"
    log.write_text("done: 1 companies (0 errors), 0 new matches, 0 auto-blocked on sponsorship, 0 queued, 0 alerts, 0 closed, 1 reopened, 0 reposts, 0 pruned.", encoding="utf-8")
    before = {p: p.read_bytes() for p in (queue, state, config, shard, log)}
    output, markdown = tmp_path / "health.json", tmp_path / "summary.md"
    markdown.write_text("Earlier step\n", encoding="utf-8")
    args = ["--queue", str(queue), "--state", str(state), "--config", str(config),
            "--shards", str(shards), "--scout-log", str(log), "--output", str(output), "--summary", str(markdown)]
    assert health.main(args) == 0
    first = json.loads(output.read_text(encoding="utf-8"))
    assert first["counts"]["missing_fetches"] == 1 and first["counts"]["reopened_this_run"] == 1
    assert health.main(args) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["last_success"]["Good"]
    assert len(output.read_bytes()) < 4000
    assert markdown.read_text(encoding="utf-8").startswith("Earlier step\n")
    assert {p: p.read_bytes() for p in before} == before


def test_cli_reports_broken_inputs_and_missing_shards_without_faking_success(tmp_path, capsys):
    queue, state, config = tmp_path / "queue.json", tmp_path / "state.json", tmp_path / "config.json"
    queue.write_text("broken", encoding="utf-8")
    state.write_text("[]", encoding="utf-8")
    config.write_text('{"companies":[{"name":"Missing"}]}', encoding="utf-8")
    output = tmp_path / "health.json"
    args = ["--queue", str(queue), "--state", str(state), "--config", str(config),
            "--shards", str(tmp_path / "absent"), "--scout-log", str(tmp_path / "absent.log"), "--output", str(output)]
    assert health.main(args) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert len(result["input_errors"]) == 4
    assert result["counts"]["companies_fetched_ok"] == 0 and not result["scout_finished"]
    assert "Input problems" in capsys.readouterr().out


def test_naive_time_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        health.build_summary([], {}, {}, now=datetime(2026, 10, 3))
