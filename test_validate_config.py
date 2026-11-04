"""Small offline fixtures for config integrity and board identity checks."""
import json

import pytest

import validate_config as vc


def company(name="Acme", ats="workday", **fields):
    return dict(name=name, ats=ats, **fields)


@pytest.fixture
def board():
    return company(host="acme.wd1.myworkdayjobs.com", tenant="acme", site="Careers")


@pytest.fixture
def queue():
    return [{"company": "Acme"}, {"company": "Acme", "closed_on": "2026-01-01"}, {"company": "Other"}]


@pytest.mark.parametrize("ats,fields", vc.REQUIRED_FIELDS.items())
def test_each_ats_accepts_minimal_fields(ats, fields):
    values = {field: "value" for field in fields}
    if ats == "icims":
        values["host"] = "careers.example.com"
    assert vc.validate({"companies": [company(ats=ats, **values)]}, []) == ([], [])


@pytest.mark.parametrize("ats,field", [(ats, f) for ats, fields in vc.REQUIRED_FIELDS.items() for f in fields])
def test_missing_required_field_fails(ats, field):
    values = {f: "value" for f in vc.REQUIRED_FIELDS[ats] if f != field}
    errors, _ = vc.validate({"companies": [company(ats=ats, **values)]}, [])
    assert any(f"missing or invalid {field} for {ats}" in error for error in errors)


@pytest.mark.parametrize("field,value", [
    ("host", None), ("host", " "), ("tenant", []), ("site", []),
    ("site", ["Careers", ""]), ("site", ["/"]), ("site", 1), ("site", {}),
])
def test_invalid_workday_fields_fail(board, field, value):
    board[field] = value
    assert vc.validate({"companies": [board]}, [])[0]


def test_names_normalize_case_spacing_punctuation_and_unicode():
    config = {"companies": [company(" Acme, Inc. ", "greenhouse", slug="one"),
                             company("ＡＣＭＥ INC", "greenhouse", slug="two")]}
    errors, groups = vc.validate(config, [{"company": "Acme Inc"}] * 2)
    assert len(errors) == 1
    assert groups[0]["kind"] == "duplicate name"
    assert groups[0]["rows"] == 2  # Do not double-count the same queue label.
    assert [m[2] for m in groups[0]["members"]] == [2, 2]


def test_workday_overlap_and_counts_include_closed_rows(board, queue):
    board["site"] = ["Careers", "Subsidiary"]
    other = company("Other", host="ACME.wd1.myworkdayjobs.com.", tenant="acme", site="/Subsidiary/", search=["different"])
    errors, groups = vc.validate({"companies": [board, other]}, queue)
    assert len(errors) == 1 and len(groups) == 1
    assert groups[0]["endpoint"].endswith("/acme/subsidiary")
    assert groups[0]["rows"] == 3
    assert [m[2] for m in groups[0]["members"]] == [2, 1]


@pytest.mark.parametrize("field,value", [("tenant", "other"), ("site", ["Separate"])])
def test_workday_disjoint_boards_are_valid(board, field, value):
    other = dict(board, name="Other")
    other[field] = value
    assert vc.validate({"companies": [board, other]}, []) == ([], [])


@pytest.mark.parametrize("field,value", [("host", "acme.wd5.myworkdaysite.com"), ("site", "careers")])
def test_workday_host_and_site_case_aliases_collide(board, field, value):
    other = dict(board, name="Other")
    other[field] = value
    errors, groups = vc.validate({"companies": [board, other]}, [])
    assert len(groups) == 1 and groups[0]["kind"] == "shared endpoint"


def test_each_overlap_is_reported_without_implying_disjoint_sites_share_board(board):
    boards = [dict(board, name="A", site=["one", "two"]),
              dict(board, name="B", site=["two", "three"]),
              dict(board, name="C", site="three")]
    _, groups = vc.validate({"companies": boards}, [])
    assert len(groups) == 2
    assert {frozenset(m[1] for m in g["members"]) for g in groups} == {frozenset(("A", "B")), frozenset(("B", "C"))}


def test_repeated_site_does_not_collide_with_itself(board):
    board["site"] = ["Careers", "Careers"]
    assert vc.validate({"companies": [board]}, []) == ([], [])


@pytest.mark.parametrize("ats,fields", [
    ("greenhouse", {"slug": "shared"}), ("oracle", {"host": "h", "site": "s"}),
    ("brassring", {"host": "h", "partnerid": "1", "siteid": "2"}),
    ("goldman", {}), ("jibe", {"host": "h"}), ("rippling", {"slug": "s"}), ("careerpuck", {"slug": "s"}), ("bamboohr", {"slug": "s"}),
    ("sitecore_search", {"discover_id": "1", "api_key": "k", "entity": "e"}),
])
def test_shared_non_workday_endpoints_fail(ats, fields):
    errors, groups = vc.validate({"companies": [company("A", ats, **fields), company("B", ats, **fields)]}, [])
    assert len(errors) == 1 and groups[0]["kind"] == "shared endpoint"


def test_taleo_listing_identity_uses_portal_not_detail_section():
    boards = [company("A", "taleo", host="h", portal="1", section="one"),
              company("B", "taleo", host="h", portal="1", section="two")]
    assert vc.validate({"companies": boards}, [])[1][0]["endpoint"] == "taleo/h/1"


def test_lever_regions_are_distinct():
    boards = [company("A", "lever", slug="shared"), company("B", "lever", slug="shared", region="eu")]
    assert vc.validate({"companies": boards}, []) == ([], [])


def test_icims_host_list_overlap():
    boards = [company("A", "icims", hosts=["first", "shared"]), company("B", "icims", host="SHARED")]
    assert vc.validate({"companies": boards}, [])[1][0]["endpoint"] == "icims/shared"


@pytest.mark.parametrize("fields", [{}, {"hosts": []}, {"hosts": "host"}, {"hosts": [None]}])
def test_icims_requires_host_or_valid_hosts(fields):
    assert vc.validate({"companies": [company(ats="icims", **fields)]}, [])[0]


@pytest.mark.parametrize("config", [[], {}, {"companies": {}}, {"companies": [None]},
                                    {"companies": [{"ats": "goldman"}]},
                                    {"companies": [{"name": "A", "ats": "unknown"}]}])
def test_malformed_config_fails(config):
    assert vc.validate(config, [])[0]


@pytest.mark.parametrize("queue", [{}, [None], [{"company": ""}]])
def test_malformed_queue_fails(board, queue):
    assert vc.validate({"companies": [board]}, queue)[0]


def test_cli_fails_prints_counts_and_leaves_files_unchanged(tmp_path, board, queue, capsys):
    config_path, queue_path = tmp_path / "config.json", tmp_path / "queue.json"
    config_path.write_text(json.dumps({"companies": [board, dict(board, name="Other")]}), encoding="utf-8")
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    before = [p.read_bytes() for p in (config_path, queue_path)]
    assert vc.main(["--config", str(config_path), "--queue", str(queue_path)]) == 1
    output = capsys.readouterr().out
    assert "3 queue rows" in output and "Acme: 2 queue rows" in output and "Other: 1 queue rows" in output
    assert [p.read_bytes() for p in (config_path, queue_path)] == before


def test_cli_success_and_bad_json(tmp_path, capsys):
    config_path, queue_path = tmp_path / "config.json", tmp_path / "queue.json"
    config_path.write_text('{"companies": []}', encoding="utf-8")
    queue_path.write_text("[]", encoding="utf-8")
    args = ["--config", str(config_path), "--queue", str(queue_path)]
    assert vc.main(args) == 0
    config_path.write_text("{", encoding="utf-8")
    assert vc.main(args) == 1
    assert "Config validation failed" in capsys.readouterr().out
