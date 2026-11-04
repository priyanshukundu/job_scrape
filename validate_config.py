#!/usr/bin/env python3
"""Read-only config checks. Exit nonzero before scouting ambiguous boards."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import unicodedata


# Required by the fetchers. Fields with fetcher defaults stay optional.
REQUIRED_FIELDS = {
    "greenhouse": ("slug",), "lever": ("slug",), "ashby": ("slug",),
    "smartrecruiters": ("slug",), "personio": ("slug",),
    "workday": ("host", "tenant", "site"), "oracle": ("host", "site"),
    "successfactors": ("host",), "icims": (),
    "eightfold": ("host", "domain"), "talentbrew": ("host",),
    "avature": ("host", "list_path"),
    "brassring": ("host", "partnerid", "siteid"),
    "jibe": ("host",), "taleo": ("host", "section", "portal"),
    "hrmdirect": ("host",), "goldman": (), "deshaw": (),
    "commerzbank": (), "dzbank": (), "marketaxess": (), "balyasny": (), "tal": ("host",), "dayforce": ("namespace", "board"), "rippling": ("slug",), "careerpuck": ("slug",), "bamboohr": ("slug",), "sitecore_search": ("discover_id", "api_key", "entity"), "phenom": ("host", "ref_num", "page_id"),
    "kpmg": (), "adp_rm": ("domain",), "adp_wfn": ("cid",), "ukg": ("board",),
    "taleo_be": ("host", "search_id", "site_name"), "taleo_cws": ("host", "path", "org", "cws"), "csod": ("corp",), "peoplesoft_hrs": ("host",), "talentsoft": ("host",), "bpce": ("host",), "paylocity": ("board",),
}
ROOT = Path(__file__).resolve().parent


def normalize_name(name):
    """Ignore Unicode compatibility, case, spacing and punctuation, not aliases."""
    return "".join(c for c in unicodedata.normalize("NFKC", name).casefold()
                   if c.isalnum())


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and bool(value) and all(_text(v) for v in value)


def _host(value):
    return value.strip().lower().rstrip(".")


def _path(value):
    # Paths and tenant identifiers are case-sensitive; DNS names are not.
    return value.strip().strip("/")


def endpoints(company):
    """One identity per actual board, excluding search terms and result caps."""
    ats = company["ats"]
    if ats == "workday":
        sites = company["site"]
        sites = sites if isinstance(sites, list) else [sites]
        # Host aliases (myworkdayjobs.com vs myworkdaysite.com) and site-path case reach one board.
        return {(ats, _path(company["tenant"]).lower(), _path(s).lower())
                for s in sites}
    if ats == "icims":
        hosts = company.get("hosts") or [company["host"]]
        return {(ats, _host(h)) for h in hosts}
    if ats == "lever":
        return {(ats, "eu" if company.get("region") == "eu" else "global", _path(company["slug"]))}
    if "slug" in REQUIRED_FIELDS[ats]:
        return {(ats, _path(company["slug"]))}
    if ats == "taleo":
        # section affects detail links, but the listing API is keyed by portal.
        return {(ats, _host(company["host"]), str(company["portal"]).strip())}
    fields = {
        "oracle": ("site",), "eightfold": ("domain",),
        "avature": ("list_path",), "taleo_be": ("search_id",), "taleo_cws": ("path", "org", "cws"), "brassring": ("partnerid", "siteid"),
    }.get(ats, ())
    if ats == "dayforce":
        return {(ats, _path(str(company["namespace"])), _path(str(company["board"])))}
    if ats in ("adp_rm", "adp_wfn", "ukg", "paylocity", "csod", "sitecore_search"):  # host-less boards keyed by their own id
        return {(ats, _path(str(company[REQUIRED_FIELDS[ats][0]])))}
    if "host" in REQUIRED_FIELDS[ats]:
        return {(ats, _host(company["host"]), *(_path(str(company[f])) for f in fields))}
    # These fetchers use one hardcoded board regardless of company name.
    return {(ats,)}


def validate(config, queue):
    """Return all errors and collision groups without importing the scout."""
    if not isinstance(config, dict) or not isinstance(config.get("companies"), list):
        return ["config.companies must be a list"], []
    if not isinstance(queue, list) or any(not isinstance(row, dict) or not _text(row.get("company")) for row in queue):
        return ["queue must be a list of objects with nonempty company names"], []
    errors, groups = [], []
    names, boards = defaultdict(list), defaultdict(list)
    row_counts = Counter(normalize_name(row["company"]) for row in queue)
    companies = config["companies"]
    for index, company in enumerate(companies):
        label = f"companies[{index}]"
        if not isinstance(company, dict):
            errors.append(f"{label} must be an object")
            continue
        name, ats = company.get("name"), company.get("ats")
        if not _text(name) or not normalize_name(name):
            errors.append(f"{label}: missing or invalid name")
        else:
            label += f" ({name})"
            names[normalize_name(name)].append(index)
        if not isinstance(ats, str) or ats not in REQUIRED_FIELDS:
            errors.append(f"{label}: missing or unsupported ats {ats!r}")
            continue
        invalid = False
        for field in REQUIRED_FIELDS[ats]:
            value = company.get(field)
            valid = _text(value)
            if field in ("partnerid", "siteid", "portal"):
                valid = valid or (isinstance(value, int) and not isinstance(value, bool) and value > 0)
            if ats == "workday" and field == "site":
                valid = valid or _strings(value)
            if not valid or (isinstance(value, str) and not _path(value)):
                errors.append(f"{label}: missing or invalid {field} for {ats}")
                invalid = True
        if ats == "icims":
            if "hosts" in company and not _strings(company["hosts"]):
                errors.append(f"{label}: hosts must be a nonempty list of host strings")
                invalid = True
            elif not company.get("hosts") and not _text(company.get("host")):
                errors.append(f"{label}: icims requires host or hosts")
                invalid = True
        if ats == "workday" and _strings(company.get("site")) and any(not _path(s) for s in company["site"]):
            errors.append(f"{label}: site paths must be nonempty")
            invalid = True
        if not invalid and _text(name):
            for endpoint in sorted(endpoints(company)):
                boards[endpoint].append(index)

    for kind, mapping in (("duplicate name", names), ("shared endpoint", boards)):
        for key, indices in sorted(mapping.items()):
            if len(indices) < 2:
                continue
            members = [(i, companies[i]["name"], row_counts[normalize_name(companies[i]["name"])])
                       for i in indices]
            # Count each normalized queue label once, even for duplicate config names.
            total = sum(row_counts[n] for n in {normalize_name(companies[i]["name"]) for i in indices})
            identity = "/".join(key) if isinstance(key, tuple) else key
            groups.append(dict(kind=kind, endpoint=identity, members=members, rows=total))
            errors.append(f"{kind}: {identity}")
    return errors, groups


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "data/config.json")
    parser.add_argument("--queue", type=Path, default=ROOT / "data/queue.json")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        queue = json.loads(args.queue.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"Config validation failed: {exc}")
        return 1
    errors, groups = validate(config, queue)
    for group in groups:
        print(f"{group['kind']}: {group['endpoint']} ({group['rows']} queue rows)")
        for index, name, count in group["members"]:
            print(f"  companies[{index}] {name}: {count} queue rows")
    grouped = {f"{g['kind']}: {g['endpoint']}" for g in groups}
    for error in errors:
        if error not in grouped:
            print(f"ERROR: {error}")
    if errors:
        print(f"Config validation failed: {len(errors)} issue(s). No files changed.")
        return 1
    print(f"Config valid: {len(config['companies'])} companies; no duplicate names or shared endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
