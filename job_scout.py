#!/usr/bin/env python3
"""
job_scout.py - free job discovery. Polls public ATS job APIs (Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, Oracle Recruiting Cloud, Personio)
for a list of target companies - no paid scraping API, no LLM tokens.

Standalone version: unlike the private-repo original, this build has no
dependency on an application tracker.

For every NEW role that matches the title/location filters it:
  1. fetches the full description and classifies sponsorship (Blocked /
     Unclear - never "Allowed", since absence of blocking language proves
     nothing either way);
  2. appends it to the triage queue and sends a phone alert (ntfy) unless
     it was auto-blocked on sponsorship.

Every run also diffs each company's current listing against its queued
entries: an id absent for CLOSE_AFTER_MISSES consecutive complete (not
paginated/capped) runs is marked closed; an id that reappears is reopened;
and a new id matching a closed entry's (title, location) fingerprint is
recorded as a repost of it (a same-role Workday req number reappearing
under a new id is common on repost, unlike Greenhouse/Lever, which usually
just reissue the same id) - only alerting if the prior posting had been
auto-blocked and the new one isn't.

Usage:
    python job_scout.py --init              create data/config.json
    python job_scout.py --check             per-company job/match counts, no state change
    python job_scout.py --dry-run           print what would be queued/sent, no state change
    python job_scout.py                     normal run (schedule this)
    python job_scout.py --queue [--all] [--json]   show the triage queue (default: state=new only)
    python job_scout.py --mark <id> <new|shortlisted|applied|dismissed|closed|open>

Runtime files live in ./data/: config.json (companies/filters - the ntfy
topic is NOT stored here, see notify() below), state.json, queue.json.
Override the data dir with JOB_SCOUT_HOME.
"""
import argparse
import html
import hashlib
from http.client import CannotSendRequest, HTTPException, HTTPSConnection, RemoteDisconnected
import io
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

import atomic_json  # noqa: E402
from keyword_matching import contains_any, contains_term  # noqa: E402

HOME = Path(os.environ.get("JOB_SCOUT_HOME", REPO_DIR / "data"))
UA = {"User-Agent": "job-scout/1.0", "Accept": "application/json"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
RETRY_429_MAX = 4  # Workday 429s on bursts; a short backoff clears them (40 of 40 recovered in testing)
QUEUE_STATES = ("new", "shortlisted", "applied", "dismissed", "opt_only", "auto_blocked")

CLOSE_AFTER_MISSES = 2     # consecutive complete-listing runs an id must be absent before we call it closed
REPOST_WINDOW_DAYS = 270   # a same-fingerprint match older than this (measured from when the prior
                           # posting actually went quiet, not from when it was first seen - see
                           # _find_repost_source) is a coincidence, not a repost
MASS_VANISH_GUARD = 0.5    # if more than this fraction of a company's open entries vanish in one run,
                           # treat it as a broken fetch (bad slug, API change) and skip closing anything

GHOST_MIN_COUNT = 3        # a role posted at least this many times under the same fingerprint...
GHOST_MIN_SPAN_DAYS = 120  # ...spanning at least this many days looks like an evergreen/ghost req,
                           # not a normal one-off backfill after someone leaves

FLOOD_MIN = 10             # a company that has run before and suddenly shows more than max(FLOOD_MIN, FLOOD_FRACTION of its
FLOOD_FRACTION = 0.5       # matches) as new is almost surely an id-scheme or search-term change, not real postings: queue
                           # them silently and send one summary instead of one alert per job
ALERT_MAX_ATTEMPTS = 3     # failed alerts are retried on later runs until this many total attempts
ALERT_CAP_PER_RUN = 30     # per-run ceiling on individual alerts; the rest queue silently and roll into one summary
ENRICH_RETRY_BUDGET = 40    # per run: failed description fetches and undated entries of DETAIL_DATED_ATS retried at most this many times
DETAIL_DATED_ATS = frozenset({"workday", "icims", "avature", "kpmg", "adp_rm", "talentsoft"})  # describe() can supply the posting date
ENRICH_MAX_ATTEMPTS = 5    # then the entry is left as it is (one try per day)
RECALL_MAX = 150           # --recall queues at most this many never-queued matching roles per run
PRUNE_AFTER_DAYS = 60      # queue.json only ever accumulates (matches() re-runs at intake, never
                           # retroactively against what's already queued), so without a cutoff it
                           # grows forever and the generated docs/index.html gets slow to render.
                           # Measured against the live queue on 2026-09-27: 60 days drops ~26% of
                           # entries and lines up with how job hunting actually works - most listings
                           # either fill or go quiet within 4-8 weeks of the employer's own posted
                           # date, so anything older is unlikely to still be an actively-screening
                           # req. 30 days was too aggressive (cuts ~47%, including postings still in
                           # a normal review cycle); 90+ days barely trims the queue at all.

# Workday's detail-endpoint startDate is not the fixed value it looks like - confirmed live: the
# same unedited posting returned startDate one calendar day apart in two checks made hours apart,
# so it drifts across Workday's own day boundary the same way postedOn's relative text does, just
# with finer granularity. An entry captured right at the boundary can freeze the wrong date forever
# unless it's re-checked once the drift window has passed. REVERIFY_MIN_AGE_DAYS gives Workday's
# clock time to settle before re-checking; REVERIFY_MAX_AGE_DAYS bounds how long a not-yet-reverified
# entry stays eligible, so this never re-fetches the whole queue on every run.
REVERIFY_MIN_AGE_DAYS = 1
REVERIFY_MAX_AGE_DAYS = 3

BLOCK_PATTERNS = [
    # Same-sentence character window (bounded by the next period, like the other patterns
    # below), not a strict word count - a strict {0,3}-word cap missed real phrasing like
    # "do not offer any type of employment-based immigration sponsorship" or "will not provide
    # any assistance ... in support of any other form of immigration sponsorship", both of which
    # put well over 3 words between the negation and "sponsor".
    r"(?:will|would|does|do|can|could|is|are)\s+not\s+[^.]{0,150}?sponsor(?!ed\s+by\s+agenc|\s+new\s+industry)",
    r"\bcannot\s+(?:\w+\s+){0,3}sponsor",
    r"unable\s+to\s+(?:\w+\s+){0,3}sponsor",
    r"\bno\s+(?:work\s+)?(?:visa\s+)?sponsorship",
    r"visa\s+sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"not\s+(?:available|offered|provided)\s+for[^.]{0,60}sponsorship",
    r"\bsponsorship\s+(?:is\s+|are\s+)?not\s+(?:available|offered|provided)",
    r"\bsponsorship\s+offered\W{0,3}no\b",
    r"\bvisa\s+sponsorship\s*\??\W{0,3}no\b",
    r"\bwithout\s+(?:current\s+)?(?:or\s+future\s+)?(?:visa\s+)?sponsorship",
    r"sponsor[^.]{0,120}(?:now|currently|current)[^.]{0,20}(?:or|and)\s+in\s+the\s+future",
    r"(?:now|currently|current)[^.]{0,20}(?:or|and)\s+in\s+the\s+future[^.]{0,120}sponsor",
    r"not\s+eligible\s+for[^.]{0,80}(?:h-?1b|employer.sponsored|sponsorship|work\s+visa)",
    r"without\s+the\s+need\s+for[^.]{0,150}(?:sponsor|employer.sponsored|employer\s+support)",
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen",
    r"u\.?s\.?\s+citizens?\s+(?:only|required)",
    r"(?:u\.?s\.?\s+citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
    r"requires?\s+permanent\s+(?:work\s+)?authorization\s+to\s+work",
    r"(?:limited|restricted)\s+to\s+(?:persons|people|individuals|candidates|applicants)\s+with\s+(?:an?\s+)?(?:indefinite|unrestricted|permanent)\s+(?:right|authori[sz]ation)\s+to\s+work",
    r"not\s+eligible\s+for[^.]{0,150}\b(?:opt|cpt)\b",
    # "immigration support/assistance" wording that never says "sponsor" (USAA 2026-10-03: "do not apply ...
    # if you will need immigration support (H-1B, TN, STEM OPT)")
    r"(?:do\s+not|don'?t|should\s+not)\s+apply[^.]{0,150}(?:immigration|visa|sponsor|work\s+authorization)",
    r"\bno\s+(?:\w+\s+)?(?:immigration|visa)\s+(?:support|assistance|sponsorship)",
    r"(?:not|unable|cannot|can'?t)\s+(?:\w+\s+){0,4}(?:offer|provide|available|support|assist|extend)[^.]{0,60}(?:immigration|visa)\s+(?:support|assistance|services)",
    r"not\s+eligible\s+for[^.]{0,80}(?:immigration|visa)\s+(?:support|assistance)",
    r"requir\w*\s+(?:\w+\s+){0,3}(?:immigration|visa|employer)\s+(?:support|assistance|sponsorship)[^.]{0,150}(?:not\s+(?:be\s+)?(?:considered|eligible)|ineligible)",
    r"(?:authorized|eligible)\s+to\s+work[^.]{0,80}without[^.]{0,60}(?:sponsor|employer\s+support|visa\s+support|immigration)",
]
BLOCK_RES = [re.compile(p, re.I) for p in BLOCK_PATTERNS]

# Wording that rules out OPT/STEM OPT too (citizenship, clearance, permanent work authorization, or an
# explicit OPT/CPT exclusion). Anything else in BLOCK_PATTERNS only refuses employer sponsorship, which
# a candidate on F-1 OPT / STEM OPT does not need, so it classifies as "OPT Only" instead of "Blocked".
HARD_BLOCK_PATTERNS = [
    r"\bbe\s+(?:an?\s+)?EU\s+citizen",
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u[.\s]{0,2}s[.\s]{0,2}\s*citizen",  # abbreviation periods are already spaces after normalization
    r"u[.\s]{0,2}s[.\s]{0,2}\s*citizens?\s+(?:only|required)",
    r"(?:u[.\s]{0,2}s[.\s]{0,2}\s*citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
    r"requires?\s+permanent\s+(?:work\s+)?authorization\s+to\s+work",
    r"(?:limited|restricted)\s+to\s+(?:persons|people|individuals|candidates|applicants)\s+with\s+(?:an?\s+)?(?:indefinite|unrestricted|permanent)\s+(?:right|authori[sz]ation)\s+to\s+work",
    r"not\s+eligible\s+for[^.]{0,150}\b(?:opt|cpt)\b",
]
_US = r"u[.\s]{0,2}s[.\s]{0,2}"  # "U.S." after abbreviation normalization
HARD_BLOCK_PATTERNS += [
    r"\b(?:top\s+secret|secret|ts/sci)\s+(?:level\s+)?(?:security\s+)?clearance[^.]{0,60}\b(?:required|must|necessary)",
    r"\b(?:must|required|requires?|need)[^.]{0,60}\b(?:top\s+secret|secret|ts/sci)\s+(?:level\s+)?(?:security\s+)?clearance",
    r"\bactive\s+(?:" + _US + r")?\s*(?:top\s+secret|secret|ts/sci)\b",
    r"\bonly\b[^.]{0,80}\b" + _US + r"\s*persons",
    _US + r"\s*persons?\s+only",
    _US + r"citizens?\s+or\s+(?:lawful\s+)?permanent\s+residents",
    r"requires?\s+" + _US + r"\s*citizenship",
    r"permanent\s+or\s+ongoing\s+work\s+authori[sz]ation",
    # DTFS 2026-10-14: "legally authorized to work permanently in the country of posting"
    r"authori[sz]ed\s+to\s+work\s+(?:\w+\s+){0,3}(?:permanently|indefinitely)",
    r"permanently\s+authori[sz]ed\s+to\s+work",
    r"authori[sz]ed\s+to\s+work[^.]{0,60}\bon\s+a\s+permanent\s+basis",
    r"\b(?:must|required|need)[^.]{0,60}\b(?:permanent|indefinite)\s+(?:right\s+to\s+work|(?:work\s+)?authori[sz]ation)",
    r"\b(?:cpt|opt)\b[^.]{0,40}\b(?:are|is)\s+restricted",
    r"(?:not|n't)\s+[^.]{0,80}?\bparticipate\s+in[^.]{0,30}\b(?:opt|cpt)\b",  # PNC 2026-10-17
]
HARD_BLOCK_RES = [re.compile(p, re.I) for p in HARD_BLOCK_PATTERNS]
_OPT_WORD_RE = re.compile(r"\b(?:opt|cpt|f-?1)\b", re.I)  # bare "stem" is a degree field, not an OPT mention
# "won't sign OPT/CPT paperwork" (JPMorgan, Mizuho) is a corner case kept as OPT Only; a bare OPT exclusion list blocks.
_SIGNOFF_RE = re.compile(r"documentation|\bsign(?:ing)?\b|execution of|execute (?:any|the)|administrative assistance|any assistance", re.I)
OPT_WINDOW_CHARS = 400  # an OPT/STEM/F-1 mention this close to a no-sponsorship phrase is read as an OPT exclusion

# Abbreviations whose internal periods break the "[^.]{0,N}" same-sentence
# windows above (a period-based window can't span "U.S." without this) -
# normalized out of the text before matching, in classify_sponsorship() only.
_ABBREV_RE = re.compile(r"\bU\.S\.(?:A\.)?|\bU\.K\.", re.I)  # no trailing \b: it never matches before a space


def _normalize_abbreviations(text):
    """Replaces each abbreviation's internal periods with spaces (never removes
    characters), so match offsets into the ORIGINAL text still line up - lets
    classify_sponsorship() slice the evidence excerpt straight out of `text`."""
    return _ABBREV_RE.sub(lambda m: m.group(0).replace(".", " "), text)


# ---------------------------------------------------------------- http / text

TITLE_TERMS = []  # filters.title_include, set from config at startup; every search-based fetcher queries these same terms


def _terms(c):
    """Search terms for a search-capable fetcher. A company whose config has a `search` value is searched with the
    one global title list (filters.title_include), never a per-company list, so every site is queried for every
    title the user asked for. No `search` key means the fetcher pulls the whole board."""
    if c.get("_terms"):  # a chunk handed down by _parallel_terms
        return list(c["_terms"])
    return list(TITLE_TERMS) if (c.get("search") and TITLE_TERMS) else (c.get("search") or [""])


def _pool_map(fn, items, workers):
    """fn over items side by side, results in item order (pages are independent once the page offsets are known)."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(items)))) as pool:
        return list(pool.map(fn, items))


def _parallel_terms(fetch=None, board=None, default_workers=3):
    """Runs a term-by-term fetcher over round-robin chunks of the title list in parallel threads and merges the rows
    (first id wins, complete only if every chunk was). 78 sequential searches took 8 to 15 minutes on Deloitte, HSBC
    and KPMG; config `term_workers` sets the thread count (default 3 above 20 terms, 1 otherwise).
    `board(c)` is an optional whole-board reader tried first when there are several terms: it returns (rows, complete)
    when the board fits under config `full_board_max` (default 1900), or None to fall back to the term search. A
    board read in a few parallel pages is far cheaper than 78 searches and returns a superset, so the title list is
    then applied only by the page filter (same trade as workday())."""
    if fetch is None:
        return lambda f: _parallel_terms(f, board, default_workers)

    def wrapped(c):
        terms = _terms(c)
        if board and len(terms) > 1 and terms != [""]:
            try:
                got = board(c)
            except Exception:  # noqa: BLE001  probe failed: fall back to the term search
                got = None
            if got is not None:
                return got
        workers = max(1, min(int(c.get("term_workers", default_workers if len(terms) > 20 else 1)), len(terms)))
        if workers == 1 or terms == [""]:
            return fetch(c)
        chunks = [terms[i::workers] for i in range(workers)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda ch: fetch(dict(c, _terms=ch)), chunks))
        out, seen, complete = [], set(), True
        for rows, ok in results:
            for j in rows:
                if j["id"] not in seen:
                    seen.add(j["id"])
                    out.append(j)
            complete = complete and ok
        return out, complete
    wrapped.__name__, wrapped.__doc__ = fetch.__name__, fetch.__doc__
    return wrapped


def _load_title_terms(cfg):
    global TITLE_TERMS
    TITLE_TERMS = [t for t in dict.fromkeys((cfg.get("filters") or {}).get("title_include", [])) if t]



_CONN_LOCAL = threading.local()  # per-thread keep-alive connections: a new TLS handshake per request cost ~100 ms each
_PROXIED = bool(urllib.request.getproxies().get("https"))


def _http_once(req, url, body, headers, timeout):
    """One request, reusing this thread's keep-alive connection to the host. Falls back to plain urlopen for
    non-https URLs, proxies and redirects. Raises urllib's own error types so callers see no difference."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or _PROXIED:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    conns = _CONN_LOCAL.__dict__.setdefault("conns", {})
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    method = "POST" if body is not None else "GET"
    for attempt in (0, 1):
        conn = conns.get(parts.netloc)
        reused = conn is not None
        if conn is None:
            conn = conns[parts.netloc] = HTTPSConnection(parts.netloc, timeout=timeout)
        elif conn.sock is not None:
            conn.sock.settimeout(timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except (RemoteDisconnected, CannotSendRequest, BrokenPipeError, ConnectionResetError):
            conn.close()
            conns.pop(parts.netloc, None)
            if reused and attempt == 0:
                continue  # the server closed an idle connection: retry once on a fresh one
            raise
        except (ConnectionError, TimeoutError, HTTPException):
            conn.close()
            conns.pop(parts.netloc, None)
            raise
        except OSError as e:  # DNS or TLS failure: urlopen reports these as URLError, which callers catch
            conn.close()
            conns.pop(parts.netloc, None)
            raise urllib.error.URLError(e) from e
        if resp.will_close:
            conn.close()
            conns.pop(parts.netloc, None)
        if 300 <= resp.status < 400:  # let urllib follow redirects
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.msg, io.BytesIO(data))
        return data


def http(url, data=None, headers=None, timeout=20, raw=False):
    h = dict(UA)
    body = None
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    elif raw:
        body = data
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    for attempt in range(RETRY_429_MAX + 1):
        try:
            payload = _http_once(req, url, body, h, timeout)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == RETRY_429_MAX:
                raise
            try:
                delay = float(e.headers.get("Retry-After"))
            except (TypeError, ValueError):
                delay = 2 ** (attempt + 1)
            time.sleep(delay + random.random())
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == RETRY_429_MAX:  # connection resets mid-pagination must not lose a whole deep fetch
                raise
            time.sleep(2 ** (attempt + 1) + random.random())
    return payload if raw else json.loads(payload)


_PAY_UNITS = {"year": "per year", "hour": "per hour", "month": "per month", "week": "per week"}


def _pay_text(currency, lo, hi, unit="year"):
    """Structured pay (ATS API field, JSON-LD baseSalary) as a sentence classify_salary reads like any other
    posted range: " Compensation: USD 150,000 - 225,000 per year." Empty when the figures are missing/zero
    (several boards publish 0/0 placeholders), the currency is unknown, or the unit isn't a plain pay period."""
    unit = (unit or "year").lower()
    unit = next((u for u in _PAY_UNITS if u in unit), None)
    try:
        lo, hi = float(lo or 0), float(hi or 0)
    except (TypeError, ValueError):
        return ""
    if not unit or not re.fullmatch(r"[A-Za-z]{3}", currency or "") or max(lo, hi) <= 0:
        return ""
    lo, hi = (min(lo, hi) or max(lo, hi)), max(lo, hi)
    fmt = lambda x: f"{x:,.2f}".rstrip("0").rstrip(".") if unit == "hour" else f"{x:,.0f}"  # noqa: E731
    span = fmt(lo) if lo == hi else f"{fmt(lo)} - {fmt(hi)}"
    return f" Compensation: {currency.upper()} {span} {_PAY_UNITS[unit]}."


def _jsonld_pay(raw):
    """Pay text from schema.org JobPosting baseSalary (MonetaryAmount with QuantitativeValue min/max/unitText)
    in any JSON-LD block of an HTML page; "" when the page carries none."""
    out = ""
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw or "", re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                stack.extend(v for v in (o.get("@graph"),) if v)
                sal = o.get("baseSalary")
                if isinstance(sal, dict):
                    val = sal.get("value") if isinstance(sal.get("value"), dict) else sal
                    lo = val.get("minValue") or val.get("value")
                    hi = val.get("maxValue") or val.get("value")
                    out += _pay_text(sal.get("currency") or val.get("currency"), lo, hi, val.get("unitText"))
    return out


def strip_html(text):
    text = html.unescape(text or "")
    text = html.unescape(text)  # Greenhouse double-escapes
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------- fetchers

def greenhouse(c):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    jobs = [dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], posted=(j.get("first_published") or "")[:10]) for j in d.get("jobs", [])]
    return jobs, True  # single uncapped call: always the full board


def lever(c):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = http(f"https://{host}/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in d:
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists") or []:
            desc += " " + (lst.get("text") or "") + " " + strip_html(lst.get("content", ""))
        desc += " " + (j.get("additionalPlain") or "")
        sr = j.get("salaryRange") or {}  # structured pay, only populated when the poster filled it in
        desc += _pay_text(sr.get("currency"), sr.get("min"), sr.get("max"), (sr.get("interval") or "").replace("per-", ""))
        posted = ""
        ms = j.get("createdAt")
        if isinstance(ms, (int, float)):
            # createdAt is an absolute UTC timestamp - must convert with an explicit UTC tz, not
            # date.fromtimestamp()'s local-machine tz, or the same posting gets a different date
            # depending on whether this ran on the UTC GitHub Actions cron or a local US-Eastern
            # run (confirmed: a controlled 2026-09-26T02:00 UTC timestamp came out as 09-25 local)
            posted = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
        out.append(dict(id=j["id"], title=j["text"], location=(j.get("categories") or {}).get("location", "") or "",
                        url=j["hostedUrl"], desc=desc, posted=posted))
    return out, True  # single uncapped call: always the full board


def _ashby_pay(j):
    """Salary components of Ashby's compensation block (equity/commission/bonus components are ignored)."""
    out = ""
    for comp in (j.get("compensation") or {}).get("summaryComponents") or []:
        if comp.get("compensationType") == "Salary":
            out += _pay_text(comp.get("currencyCode"), comp.get("minValue"), comp.get("maxValue"), comp.get("interval"))
    return out


def ashby(c):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true")
    jobs = [dict(id=j["id"], title=j["title"], location=j.get("location", "") or "", url=j.get("jobUrl", ""),
                 desc=(j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))) + _ashby_pay(j),
                 posted=(j.get("publishedAt") or "")[:10]) for j in d.get("jobs", [])]
    return jobs, True  # single uncapped call: always the full board


def smartrecruiters(c):
    out, offset = [], 0
    complete = False
    ceiling = int(c.get("max_postings", 2000))
    while offset < ceiling:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={offset}")
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            out.append(dict(id=j["id"], title=j["name"],
                            location=", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                            url=f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                            posted=(j.get("releasedDate") or "")[:10]))
        offset += 100
        if len(rows) < 100:
            complete = True  # ran out of rows before hitting the 2000 ceiling: this is everything
            break
    return out, complete


_RELATIVE_DAYS_RE = re.compile(r"posted\s+(today|yesterday|(\d+)(\+?)\s*day)", re.I)


_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _workday_location(text, path):
    """Workday shows "2 Locations" for multi-site postings, hiding the city from locations_exclude;
    the city is still in externalPath (/job/London/...), so append it."""
    text = text or ""
    if _MULTI_LOC_RE.match(text):
        m = re.match(r"/job/([^/]+)/", path or "")
        if m:
            return f"{text} ({m.group(1).replace('-', ' ')})"
    return text


def _workday_full_location(info):
    """Workday's detail response lists the primary site in `location` and the rest in `additionalLocations`."""
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    seen, out = set(), []
    for l in locs:
        l = (l or "").strip()
        if l and l not in seen:
            seen.add(l)
            out.append(l)
    return "; ".join(out)


def _workday_posted(text):
    """Workday's postedOn is relative text ("Posted 5 Days Ago", "Posted Today", "Posted 30+ Days
    Ago") not a real date - approximate it as an ISO date so postings can be sorted/filtered by
    recency. "30+" is a floor, not a real day count (Workday caps the display at that bucket, so
    the true age could be 30 days or 300) - computing today-minus-30 for it would print a specific
    date that looks exact but is actually a guess, so treat it as unknown ("") instead. This is
    meant as a last-resort fallback when the detail endpoint (which usually has the real date via
    startDate - see _workday_detail_posted) couldn't be reached at all."""
    m = _RELATIVE_DAYS_RE.search(text or "")
    if not m:
        return ""
    if m.group(1).lower() == "today":
        days = 0
    elif m.group(1).lower() == "yesterday":
        days = 1
    elif m.group(3) == "+":
        return ""  # "30+ Days Ago" - a floor, not a count; don't dress it up as a real date
    else:
        days = int(m.group(2))
    return (date.today() - timedelta(days=days)).isoformat()


def _workday_detail_posted(info):
    """Workday's job-DETAIL response (unlike the bulk search listing) usually carries a startDate -
    prefer it over the relative postedOn bucket text, and over the bulk listing's own postedOn
    (which some tenants, e.g. Ares, omit from the bulk response entirely).

    Returns (date, source) - source is "exact" for startDate, "approx" for the relative-text
    fallback (derived from a coarse day-count string, not a timestamp - see _workday_posted), or
    "unknown" for neither. Callers surface this so a date on the tracker page never looks more
    certain than it actually is.

    "exact" is still not perfectly stable, confirmed live: the same unedited posting returned
    startDate one calendar day apart in two checks made hours apart, meaning Workday recomputes it
    relative to its own server clock at request time too, the same way postedOn's relative text is,
    just with finer granularity and a much smaller/rarer drift window (one day, around Workday's own
    day boundary, vs. every single request for postedOn). That's still far more reliable than the
    relative-text fallback, so "exact" stays the right label relative to "approx" - but it's why
    _reverify_recent_posted() re-checks a freshly captured value a day or two later instead of
    trusting it permanently from the moment of first capture."""
    start = (info.get("startDate") or "")[:10]
    try:
        if start and date.fromisoformat(start) <= date.today() + timedelta(days=1):
            return start, "exact"
    except ValueError:
        pass
    fallback = _workday_posted(info.get("postedOn"))
    return fallback, ("approx" if fallback else "unknown")


def _workday_refresh_posted(c, job_id, site):
    """Re-fetches just the detail endpoint's date fields (no description/salary/etc - this only
    runs to re-verify a date, so there's no reason to re-parse and re-classify the whole posting)."""
    d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}{job_id}")
    return _workday_detail_posted(d.get("jobPostingInfo") or {})


def _probe_true_size(get_rows, page, start=2000, max_probe=None):
    """True size of a Workday board whose first-page total is 2000 or more. `get_rows(offset)` returns one page.
    Gallops from `start` doubling the offset until a page comes back empty, then bisects page-aligned offsets to the
    first empty page: about a dozen requests. Returns the size; -1 when pages past `start` just repeat page 0 (the
    offset is ignored, as on Citi and Booz Allen: the board is capped at 2000 reachable rows); None when the board is
    bigger than `max_probe`. A failed request raises."""
    first = get_rows(0)
    key = lambda r: r.get("externalPath") or r.get("title")  # noqa: E731
    first_keys = {key(r) for r in first}

    def rows_at(off):
        """Rows at an offset, [] past the end: Workday answers an offset past the end with page 0 again."""
        rows = get_rows(off)
        return [] if rows and first_keys and {key(r) for r in rows} <= first_keys else rows

    lo, lo_rows = 0, first  # highest offset known to hold rows
    hi = start  # candidate first-empty offset
    probe = get_rows(start)
    if probe and first_keys and {key(r) for r in probe} <= first_keys:
        return -1  # the board is bigger than `start` rows yet offset `start` repeats page 0: offsets are capped
    while True:
        rows = probe if hi == start else rows_at(hi)
        if not rows:
            break
        lo, lo_rows = hi, rows
        if max_probe is not None and hi > max_probe:
            return None
        hi *= 2
    while hi - lo > page:
        mid = lo + ((hi - lo) // page // 2) * page
        if mid <= lo:
            break
        rows = rows_at(mid)
        if rows:
            lo, lo_rows = mid, rows
        else:
            hi = mid
    return lo + len(lo_rows)


def _facet_parts(post, part_max=1900, max_depth=3):
    """Splits a Workday board the offset cap hides (reported total 2000, offsets past 2000 repeat page 0) into
    facet slices that each fit under the cap. `post(applied)` runs a limit-1 empty search with those appliedFacets and
    returns the response. Returns (parts, expected): parts are (appliedFacets, count) slices, expected is the sum of
    the chosen facet group's counts at the top level (what a complete read must reach), or None when the board
    exposes no usable facet. A slice still over part_max after max_depth splits stays whole and reads incomplete."""
    def groups(d, applied):
        out = []
        for f in d.get("facets") or []:
            param = f.get("facetParameter")
            vals = [v for v in f.get("values") or [] if isinstance(v.get("count"), int) and v.get("id")]
            if param and param not in applied and vals:
                out.append((param, vals))
        return out

    def best(gs):
        """The group to slice by and the posting count a complete read must reach. Facets that cover every posting
        once agree on their sum (jobFamilyGroup, workerSubType and timeType all say 2066), so the highest sum shared
        by two groups is the board size; without one, the highest sum within 1.1x the median (a facet whose values
        overlap, like states, sums far past the posting count). Among the groups that reach it, the one with the
        smallest biggest slice splits best."""
        sums = [sum(v["count"] for v in g[1]) for g in gs]
        shared = [n for n in set(sums) if sums.count(n) > 1]
        med = sorted(sums)[len(sums) // 2]
        top = max(shared) if shared else max(n for n in sums if n <= 1.1 * med)
        g = min((g for g, n in zip(gs, sums) if 0.995 * top <= n <= 1.1 * top),
                key=lambda g: max(v["count"] for v in g[1]))
        return g, top

    def split(applied, d, depth):
        gs = groups(d, applied)
        if not gs or depth >= max_depth:
            return [(applied, d.get("total") or 0)]
        param, vals = best(gs)[0]
        parts = []
        for v in vals:
            if v["count"] <= 0:
                continue
            sub = dict(applied, **{param: [v["id"]]})
            if v["count"] <= part_max:
                parts.append((sub, v["count"]))
            else:
                parts += split(sub, post(sub), depth + 1)
        return parts

    root = post({})
    gs = groups(root, {})
    if not gs:
        return None
    return split({}, root, 0), best(gs)[1]


_FETCH_DEADLINE = [None]  # set by fetch_all: epoch seconds after which the shard job's budget is spent
_LOST_PAGE_RETRY_MAX = 60  # more lost pages than this means heavy throttling: a retry pass would only add load
_SEQ_RETRY_BUDGET = [4]  # sequential re-reads allowed per process, so retries cannot stretch a run


def workday(c):
    """Unofficial endpoint. Needs host, tenant, site. 'search' is a term or a list of terms; each
    term is queried separately (max_per_term results, default 60) and results are merged by id.
    'site' may also be a list of site paths under the same tenant - some subsidiaries post to a
    separate career site sharing the parent's Workday tenant (e.g. PGIM/Prudential: same host and
    tenant, "PGIM_Careers" vs "Careers"), and the same requisition can appear on both - tracking
    them as one company lets the existing repost-fingerprint matching recognize that instead of
    silently double-counting it as two unrelated postings under two different company names."""
    sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
    terms = _terms(c)
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    ceiling = int(c.get("term_ceiling", 1000))
    out, seen = [], set()
    page = 20  # Workday's unofficial endpoint 400s on limit > 20 - confirmed by testing, not documented
    complete = True  # AND across terms/sites: one truncated one makes the whole listing untrustworthy for removal

    def post(site, term, offset, applied=None, limit=page):
        base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}/jobs"
        return http(base, data={"appliedFacets": applied or {}, "limit": limit, "offset": offset, "searchText": term})

    def get(site, term, offset, applied=""):
        return post(site, term, offset, json.loads(applied) if applied else None)

    def first_page(st):
        site, term, applied = st
        d = get(site, term, 0, applied)  # raises on failure: nothing fetched yet, so the caller's error handling applies
        rows = list(d.get("jobPostings", []))
        total = d.get("total") if isinstance(d.get("total"), int) else None  # only the first page reliably carries total
        if st in whole:  # whole-board slice: its known size rules, plus one spare page in case the board grew
            limit = whole[st] + page
            return rows, (list(range(page, limit, page)) if len(rows) >= page else []), len(rows) < page, limit, None
        limit = max(cap, min(total, ceiling)) if total is not None else cap  # page past cap, up to ceiling
        if len(rows) < page or (total is not None and page >= total):
            return rows, [], True, limit, total
        return rows, list(range(page, limit, page)), False, limit, total

    def later_page(task):
        site, term, applied, offset = task
        try:
            return get(site, term, offset, applied)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            return None  # a page lost to rate limiting: keep the rest, mark the term incomplete

    # Slow tenants opt in via config "term_workers" (RBC answers in ~5s per request, so 16 sequential
    # multi-page terms took 4 min). Default stays 1: running every tenant's terms in parallel got
    # 429s on unrelated tenants from the shared per-IP load. map() keeps term order, so output is unchanged.
    whole = {}  # combo -> exact row count of a slice that is read whole (empty search, no term)
    expected = {}  # site -> row count a facet-sliced read must reach to count as complete
    read_whole = False  # True when at least one site is read as a whole board (no term search)
    if len(terms) > 1 and terms != [""]:
        # A board that fits under Workday's 2000-result cap is cheaper to read whole than to query term by term,
        # and returns a superset, so the title list is then applied only by the page filter. A bigger board is read
        # whole too when its true size is below board_read_max: straight through when offsets keep working past 2000
        # (PwC, Lowe's, CVS), or in facet slices when they do not (Citi, PNC, Booz Allen: offsets >= 2000 repeat page 0).
        small, bigsites = [], []
        for site in sites:
            try:
                tot = post(site, "", 0, limit=1).get("total")
                if isinstance(tot, int) and tot <= int(c.get("full_board_max", 1999)):  # below 2000 the total is real
                    small.append(site)
                elif isinstance(tot, int) and tot >= 2000:
                    bmax = int(c.get("board_read_max", 25000))
                    size = _probe_true_size(lambda off, s=site: get(s, "", off).get("jobPostings") or [], page,
                                            max_probe=bmax * 2)
                    if size == -1:
                        got = _facet_parts(lambda ap, s=site: post(s, "", 0, ap, limit=1))
                        if got and got[1] <= bmax:
                            for ap, n in got[0]:
                                whole[(site, "", json.dumps(ap, sort_keys=True))] = n
                            expected[site] = got[1]
                            bigsites.append(site)
                    elif size is not None and size <= bmax:
                        whole[(site, "", "")] = size
                        bigsites.append(site)
            except Exception:  # noqa: BLE001  probe failed: fall back to the term search for this site
                whole = {k: v for k, v in whole.items() if k[0] != site}
                expected.pop(site, None)
                if site in bigsites:
                    bigsites.remove(site)
        if small or bigsites:
            cap, ceiling = 2000, 2000
            read_whole = True
        elif len(small) < len(sites):
            ceiling = min(ceiling, int(c.get("big_board_ceiling", 300)))  # 78 terms x deep paging is slow and trips 429s
        combos = []
        for site in sites:
            if site in small:
                combos.append((site, "", ""))
            elif site in bigsites:
                combos += [k for k in whole if k[0] == site]
            else:
                combos += [(site, term, "") for term in terms]
    else:
        combos = [(site, term, "") for site in sites for term in terms]
    # One flat pool of term_workers x page_workers threads serves first pages and then every remaining page of every
    # term, so short terms never leave threads idle behind a long one (nested per-term pools did).
    tw = max(1, min(int(c.get("term_workers", 3 if len(combos) > 20 else 1)), len(combos)))
    workers = tw * max(1, int(c.get("page_workers", 3)))
    if whole and all(k in whole for k in combos):  # a whole-board read is one long page queue: page_workers (default 12)
        workers = max(1, int(c.get("page_workers", 12)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        firsts = list(pool.map(first_page, combos))
        tasks = [st + (off,) for st, f in zip(combos, firsts) for off in f[1]]
        later = list(pool.map(later_page, tasks))
        lost_idx = [i for i, d in enumerate(later) if d is None]
        dl = _FETCH_DEADLINE[0]
        if 0 < len(lost_idx) <= _LOST_PAGE_RETRY_MAX and (dl is None or time.time() + 3 * len(lost_idx) < dl):
            for i in lost_idx:  # re-request only the throttled pages, gently, instead of re-reading the whole board
                time.sleep(2)
                later[i] = later_page(tasks[i])
    n_requests = len(combos) + len(tasks)
    later = iter(later)
    results = []
    for st, (rows_all, offsets, done, limit, total) in zip(combos, firsts):
        lost = exhausted = False
        have = {r.get("externalPath") for r in rows_all}
        for _off in offsets:
            d = next(later)
            if exhausted:
                continue
            if d is None:
                lost = True
                continue
            rows = d.get("jobPostings", [])
            if rows and all(r.get("externalPath") in have for r in rows):
                exhausted = True  # past the end Workday answers with page 0 again: nothing new is left
                continue
            rows_all.extend(rows)
            have.update(r.get("externalPath") for r in rows)
            if len(rows) < page:
                exhausted = True  # ran out of rows before hitting the limit
        # complete only if no page was lost and we reached the term's last result (not just the cap/ceiling)
        results.append((rows_all, done or ((not lost) and (exhausted or (total is not None and limit >= total)))))
    per_site = {}
    for (site, _term, _ap), (rows, term_complete) in zip(combos, results):
        for j in rows:
            path = j.get("externalPath")
            key = (site, path)
            if not path or not j.get("title") or key in seen:  # some rows are placeholder cards with no title/path
                continue
            seen.add(key)
            per_site[site] = per_site.get(site, 0) + 1
            out.append(dict(id=path, title=j["title"], location=_workday_location(j.get("locationsText"), path),
                            url=f"{c.get('url_base') or 'https://' + c['host']}/{site}{path}", site=site,
                            posted=_workday_posted(j.get("postedOn"))))
        complete = complete and term_complete
    for site, want in expected.items():  # facet slices must add up to the board: a short count means postings the
        if per_site.get(site, 0) < float(c.get("facet_coverage_min", 0.998)) * want:  # facet does not cover (or a mid-read change), never a complete read
            complete = False
    dl = _FETCH_DEADLINE[0]
    seq_fits = dl is None or time.time() + 0.7 * n_requests < dl - 15  # a sequential re-read that cannot finish only burns the budget
    if not complete and read_whole and not c.get("_seq_retry") and _SEQ_RETRY_BUDGET[0] > 0 and seq_fits:
        # Parallel paging on a live board repeats and skips rows (Truist/Wells Fargo read 1-3% short while a
        # one-page-at-a-time read gets all of it), so re-read a short whole-board once, sequentially, and merge.
        _SEQ_RETRY_BUDGET[0] -= 1
        retry_out, retry_complete = workday({**c, "_seq_retry": True, "page_workers": 1, "term_workers": 1})
        merged = {(j.get("site"), j["id"]): j for j in out}
        for j in retry_out:
            merged.setdefault((j.get("site"), j["id"]), j)
        return list(merged.values()), retry_complete
    return out, complete


def oracle(c):
    """Oracle Recruiting Cloud (Fusion HCM) - unofficial/undocumented endpoint. Needs host, site
    (Oracle's siteNumber, e.g. "CX_1001"). Same per-term paged search shape as workday(), but
    Oracle's quirks (reverse-engineered against a real tenant, not in any doc):
      - the search term goes in finder as "keyword=", not a top-level searchText param;
      - limit/offset MUST live inside the finder string too - Oracle silently ignores them as
        top-level query params and always returns a fixed first page of 25;
      - expand=requisitionList is required or the response has only facet metadata, no jobs."""
    base = f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    terms = _terms(c)
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    ceiling = int(c.get("term_ceiling", 1000))
    page = 50

    def fetch_term(term, cap, ceiling):
        rows_all, offset, limit, total = [], 0, cap, None
        while offset < limit:
            finder = f"findReqs;siteNumber={c['site']},limit={page},offset={offset}"
            if term:
                finder += f",keyword={term}"
            url = base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
            d = http(url)
            items = d.get("items") or [{}]
            rows = items[0].get("requisitionList") or []
            rows_all.extend(rows)
            if offset == 0 and isinstance(items[0].get("TotalJobsCount"), int):
                total = items[0]["TotalJobsCount"]
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            offset += page
            if len(rows) < page or (total is not None and offset >= total):
                return rows_all, True  # ran out of rows before hitting the limit
        return rows_all, False

    if len(terms) > 1 and terms != [""]:
        # A board small enough to read whole is cheaper than one query per term and returns a superset, so the
        # title list is then applied only by the page filter (same trade as workday()).
        try:
            d = http(base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList",
                                                          "finder": f"findReqs;siteNumber={c['site']},limit=1,offset=0"}))
            tot = ((d.get("items") or [{}])[0]).get("TotalJobsCount")
        except Exception:  # noqa: BLE001  probe failed: fall back to the term search
            tot = None
        if isinstance(tot, int) and tot <= int(c.get("full_board_max", 1900)):
            combos = [("", max(cap, 2000), 2000)]
        elif isinstance(tot, int) and tot <= int(c.get("board_read_max", 25000)):
            # a big board whose total is real (JPMorgan ~7,300): offsets work to the end, so read it once, page by
            # page in parallel, instead of 78 keyword searches; a page lost to a 429 leaves the read incomplete
            def board_page(off):
                finder = f"findReqs;siteNumber={c['site']},limit={page},offset={off}"
                url = base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList",
                                                           "finder": finder})
                try:
                    return ((http(url).get("items") or [{}])[0]).get("requisitionList") or []
                except urllib.error.HTTPError as e:
                    if e.code != 429:
                        raise
                    return None
            pages = _pool_map(board_page, range(0, tot + page, page), c.get("page_workers", 12))
            lost = any(p is None for p in pages)
            rows = [r for p in pages if p for r in p]
            combos = None
            results = [(rows, not lost)]
        else:
            # 78 terms x deep paging took over 30 min on JPMorgan, so big boards page shallower and run terms in parallel
            combos = [(t, cap, min(ceiling, int(c.get("big_board_ceiling", 300)))) for t in terms]
    else:
        combos = [(t, cap, ceiling) for t in terms]
    if combos is not None:
        workers = max(1, min(int(c.get("term_workers", 3 if len(combos) > 20 else 1)), len(combos)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda x: fetch_term(*x), combos))
    out, seen = [], set()
    complete = True  # AND across terms: one truncated term makes the whole listing untrustworthy for removal
    for rows, term_complete in results:
        for j in rows:
            jid = str(j.get("Id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append(dict(id=jid, title=j.get("Title", "") or "", location=j.get("PrimaryLocation", "") or "",
                            url=f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{jid}",
                            posted=(j.get("PostedDate") or "")[:10]))
        complete = complete and term_complete
    return out, complete


def personio(c):
    """Personio's public XML job feed - no auth, no API key. Needs 'slug' (the company subdomain
    in https://{slug}.jobs.personio.de). Verified live against a real tenant (ottonova.jobs.
    personio.de) on 2026-09-27, including a real Risk Management & Actuary posting - confirms the
    feed genuinely carries finance-relevant roles, not just this doc example.

    Each tenant must have turned the XML feed on themselves (Settings > Recruiting > Career page),
    so a candidate company with no career site on personio.de, or one that never enabled it, 404s
    or returns an empty <workzag-jobs/> - both look like "0 jobs" via --check, not an error, so
    verify a candidate by curling the URL directly before adding it to config.json, same as every
    other ATS here.

    The feed embeds the full description already (unlike Greenhouse/SmartRecruiters/Workday/
    Oracle, which need a second per-job detail call) - see the jobDescriptions/jobDescription/value
    CDATA blocks below - so describe() never needs a personio-specific branch."""
    raw = http(f"https://{c['slug']}.jobs.personio.de/xml?language=en", raw=True)
    root = ET.fromstring(raw)
    jobs = []
    for pos in root.findall("position"):
        jid = pos.findtext("id", "") or ""
        offices = [pos.findtext("office", "") or ""]
        offices += [o.text or "" for o in pos.findall("additionalOffices/office")]
        desc = " ".join(strip_html(v.text or "") for v in pos.findall("jobDescriptions/jobDescription/value"))
        jobs.append(dict(id=jid, title=pos.findtext("name", "") or "",
                         location=", ".join(o for o in offices if o),
                         url=f"https://{c['slug']}.jobs.personio.de/job/{jid}?language=en",
                         desc=desc, posted=(pos.findtext("createdAt", "") or "")[:10]))
    return jobs, True  # single uncapped call: always the full board


def goldman(c):
    """Goldman Sachs' own careers site (higher.gs.com) - unofficial GraphQL gateway, reverse-engineered
    and verified live 2026-10-02 (not in any doc). No config beyond 'search' terms. `experiences` must
    be non-empty (an empty list is a validation error); the list call already returns descriptionHtml,
    so describe() needs no goldman branch. `posted` is lastPostedDate (the real posting day; createdDate trails it by a day)."""
    terms = _terms(c)
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 100))
    ceiling = int(c.get("term_ceiling", 1000))
    page = 50
    query = ("query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: "
             "$searchQueryInput) { totalCount items { roleId jobTitle division descriptionHtml lastPostedDate createdDate "
             "compensation { minSalary maxSalary currency } locations { city state country } externalSource { sourceId } } } }")

    def call(term, size, number):
        return http("https://api-higher.gs.com/gateway/api/v1/graphql", data={
            "operationName": "GetRoles", "query": query, "variables": {"searchQueryInput": {
                "page": {"pageSize": size, "pageNumber": number},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"}, "filters": [],
                "experiences": ["PROFESSIONAL", "EARLY_CAREER"], "searchTerm": term}}})

    if len(terms) > 1:
        # The whole board (649 roles on 2026-10-11) is 4 calls at pageSize 200 (the gateway's maximum), against 78 term
        # searches of up to 4 pages each, and is a superset: the title list is then applied only by the page filter.
        try:
            tot = (((call("", 1, 0).get("data") or {}).get("roleSearch") or {}).get("totalCount"))
        except Exception:  # noqa: BLE001  probe failed: fall back to the term search
            tot = None
        if isinstance(tot, int) and tot <= int(c.get("full_board_max", 1900)):
            terms, page, cap, ceiling = [""], 200, 2000, 2000
    out, seen = [], set()
    complete = True
    for term in terms:
        offset, term_complete, limit, total = 0, False, cap, None
        while offset < limit:
            d = call(term, page, offset // page)
            if d.get("errors"):
                raise RuntimeError(f"goldman graphql: {d['errors'][0].get('message')}")
            rows = ((d.get("data") or {}).get("roleSearch") or {}).get("items") or []
            if offset == 0 and isinstance(((d.get("data") or {}).get("roleSearch") or {}).get("totalCount"), int):
                total = d["data"]["roleSearch"]["totalCount"]
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            for j in rows:
                jid = str(j.get("roleId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = [", ".join(x for x in (l.get("city"), l.get("state"), l.get("country")) if x)
                        for l in (j.get("locations") or [])]
                src = (j.get("externalSource") or {}).get("sourceId") or jid.split("_")[0]
                out.append(dict(id=jid, title=j.get("jobTitle", "") or "", location=" | ".join(locs),
                                url=f"https://higher.gs.com/roles/{src}",
                                posted=(j.get("lastPostedDate") or j.get("createdDate") or "")[:10],
                                desc=strip_html(j.get("descriptionHtml", "")) + _pay_text(
                                    (j.get("compensation") or {}).get("currency"), (j.get("compensation") or {}).get("minSalary"),
                                    (j.get("compensation") or {}).get("maxSalary"))))
            offset += page
            if len(rows) < page or (total is not None and offset >= total):
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


_SF_NO_PAGING = set()  # hosts whose RSS feed ignores startrow (page 2 repeated page 1), so a full first page is all a term gives


@_parallel_terms
def successfactors(c):
    """SAP SuccessFactors Career Site Builder tenants that expose the public RSS job feed. Needs
    host and locale (e.g. jobs.scotiabank.com / en_US). Verified live 2026-10-02 against Scotiabank,
    Standard Chartered and SMBC. Feed shape: /services/rss/job/?locale=..&keywords=(term)&startrow=N,
    20 items per page, each with the full description, link, guid and pubDate; the location is the
    trailing parenthetical of the title. Only tenants using the Career Site Builder front end have
    this feed (Moody's career8 login pages do not), so verify a host by curl before adding it."""
    terms = _terms(c)
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    page = 20
    out, seen = [], set()
    complete = True
    for term in terms:
        offset, term_complete, term_seen = 0, False, set()
        while offset < cap:
            if offset and c["host"] in _SF_NO_PAGING:
                term_complete = True  # the feed would only repeat page 1 (Scotiabank and Fitch, verified live 2026-10-11)
                break
            q = {"locale": c.get("locale", "en_US"), "startrow": offset}
            if term:
                q["keywords"] = f"({term})"
            raw = http(f"https://{c['host']}/services/rss/job/?" + urllib.parse.urlencode(q), raw=True, timeout=40,
                       headers={"Accept": "application/rss+xml, */*"})  # default json-only Accept gets a 406
            items = ET.fromstring(raw).findall("channel/item")
            fresh = 0
            for it in items:
                link = (it.findtext("link", "") or "").strip()
                parts = urllib.parse.urlsplit(link)
                link = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # drop RSS tracking params
                jid = parts.path.rstrip("/").rsplit("/", 1)[-1]
                if not jid:
                    continue
                if jid not in term_seen:
                    term_seen.add(jid)
                    fresh += 1  # fresh is per search, so a page that only repeats earlier searches still pages on
                if jid in seen:
                    continue
                seen.add(jid)
                title = (it.findtext("title", "") or "").strip()
                m = re.match(r"^(.*)\s\(([^()]*)\)$", title)
                title, loc = (m.group(1), m.group(2)) if m else (title, "")
                try:
                    posted = parsedate_to_datetime(it.findtext("pubDate", "")).date().isoformat()
                except (TypeError, ValueError):
                    posted = ""
                out.append(dict(id=jid, title=title, location=loc, url=link, posted=posted,
                                desc=strip_html(it.findtext("description", ""))))
            if offset and items and not fresh:
                _SF_NO_PAGING.add(c["host"])  # a whole page of repeats: startrow is ignored, skip page 2 for the other terms
            offset += page
            if len(items) < page or not fresh:
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


def _icims_location(text):
    """iCIMS shows "US-NY-New York" / "PH-Makati City Manila" (country-[state-]city); rewrite it as
    "New York, NY, US" so the shared location filters read it like every other ATS."""
    parts = [x.strip() for x in (text or "").split("-", 2)]
    if len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha():
        return ", ".join(reversed(parts))
    return (text or "").strip()


def icims(c):
    """iCIMS career portals (server-rendered HTML job cards, no API). Needs 'hosts' (one or more
    portal hosts, e.g. MSCI splits global/us/uk/rsa across four). Verified live 2026-10-02 against
    MSCI. List pages carry title, id, location and posted date; the full description comes from each
    job page's JSON-LD block via describe(). Paged with pr=0,1,.. until a page has no cards."""
    hosts = c.get("hosts") or [c["host"]]
    cap_pages = int(c.get("max_pages", 20))
    out, seen = [], set()
    complete = True
    for host in hosts:
        for pr in range(cap_pages):
            raw = http(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={pr}", raw=True, timeout=40,
                       headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
            cards = raw.split('class="iCIMS_JobCardItem"')[1:]
            for card in cards:
                m = re.search(r'href="(https://[^"]+/jobs/(\d+)/[^"?]*)', card)
                if not m or m.group(2) in seen:  # one posting can appear on several of a tenant's portals
                    continue
                seen.add(m.group(2))
                title = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", card, re.S)
                loc = (re.search(r"Job Locations</span>.*?<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
                       or re.search(r">Locations?</span>\s*<span[^>]*>\s*(.*?)\s*</span>", card, re.S)  # Stifel-style cards
                       or re.search(r'field-label">Location : Location</span>\s*<span[^>]*>\s*(.*?)\s*</span>', card, re.S))  # WaFd header label
                posted = re.search(r'<span title="(\d{1,2})/(\d{1,2})/(\d{4})', card)
                where = _icims_location(strip_html(loc.group(1)) if loc else "")
                if not where:  # Berkley cards carry only a company name up top; the place sits in "Primary Location"
                    pl = re.search(r"Primary Location.*?<dd[^>]*>\s*<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
                    where = _icims_location(strip_html(pl.group(1)) if pl else "")
                if not where:
                    # Lord Abbett / America First cards split the place into labelled City and State fields
                    # ("Location : City", "State/Province (Full Name)"); join them so US sites read "Roy, Utah".
                    parts = [strip_html(v) for lab, v in re.findall(
                        r'field-label">([^<]*(?:City|State)[^<]*)</span>.*?<span[^>]*>\s*(.*?)\s*</span>', card, re.S)]
                    where = ", ".join(dict.fromkeys(p for p in parts if p))
                out.append(dict(id=m.group(2), title=strip_html(title.group(1)) if title else "",
                                location=where,
                                url=m.group(1),
                                posted=f"{posted.group(3)}-{int(posted.group(1)):02d}-{int(posted.group(2)):02d}" if posted else ""))
            if not cards:
                break
        else:
            complete = False  # hit the page cap on this portal
    return out, complete


_ICIMS_SITEMAPS = {}  # host -> {job id: lastmod date}; one sitemap fetch serves every describe() call of a run


def _icims_sitemap_dates(host):
    """{job id: YYYY-MM-DD} from the portal's /sitemap.xml lastmod values ({} if unreachable)."""
    if host not in _ICIMS_SITEMAPS:
        try:
            raw = http(f"https://{host}/sitemap.xml", raw=True, timeout=40, headers={"Accept": "application/xml, */*"}).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001  a bonus source: the missing date just stays blank
            return {}
        _ICIMS_SITEMAPS[host] = {m.group(1): m.group(2) for m in re.finditer(r"/jobs/(\d+)/[^<]*</loc>\s*<lastmod>(\d{4}-\d\d-\d\d)", raw)}
    return _ICIMS_SITEMAPS[host]


def _icims_description(job):
    """(description, posted, source) from a job page's JSON-LD. The list card often has no posted date
    (Schwab's doesn't), so describe() returns the JSON-LD datePosted, which refines it for each new match.
    Some tenants (East West Bank, verified live 2026-10-11) stamp datePosted with the render time minus two
    years, which never lands on a whole minute while real values are midnight Eastern ("...T04:00:00.000Z");
    those fall back to the portal sitemap's lastmod, an update date, so it is labeled "approx"."""
    raw = http(job["url"] + "?in_iframe=1", raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        return "", "", ""
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", "", ""
    stamp = str(data.get("datePosted") or "")
    if re.match(r"\d{4}-\d\d-\d\dT\d\d:00:00(\.000)?Z?$", stamp) or re.match(r"\d{4}-\d\d-\d\d$", stamp):
        return strip_html(data.get("description", "")), stamp[:10], "exact"
    host = urllib.parse.urlparse(job["url"]).netloc
    approx = _icims_sitemap_dates(host).get(str(job["id"]), "")
    return strip_html(data.get("description", "")), approx, "approx" if approx else ""


def _eightfold_page(c, term, start):
    q = urllib.parse.urlencode({"domain": c["domain"], "start": start, "num": 10, "query": term})
    if c.get("pcsx"):  # PCSX tenants (Qualcomm) reject /api/apply/v2 with 403 "Not authorized for PCSX"
        return http(f"https://{c['host']}/api/pcsx/search?{q}", headers={"Accept": "application/json"}).get("data") or {}
    return http(f"https://{c['host']}/api/apply/v2/jobs?{q}", headers={"Accept": "application/json"})


def _eightfold_detail(c, job_id):
    """Description HTML of one position; PCSX serves it from /api/pcsx/position_details."""
    hdr = {"Accept": "application/json"}
    if c.get("pcsx"):
        d = http(f"https://{c['host']}/api/pcsx/position_details?position_id={job_id}&domain={c['domain']}&hl=en", headers=hdr).get("data") or {}
        return d.get("jobDescription", "")
    return http(f"https://{c['host']}/api/apply/v2/jobs/{job_id}?domain={c['domain']}", headers=hdr).get("job_description", "")


def _eightfold_rows(c, rows, seen, out):
    """Appends the new positions of one Eightfold list page to out."""
    for j in rows:
        jid = str(j.get("id") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        locs = j.get("locations") or [j.get("location") or ""]
        posted = ""
        stamp = j.get("t_create") or j.get("creationTs")  # PCSX postedTs is a daily refresh stamp, creationTs the real start
        if stamp:
            posted = datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat()
        rel = j.get("positionUrl")
        out.append(dict(id=jid, title=j.get("name", "") or "", location=" | ".join(str(x) for x in locs if x),
                        url=j.get("canonicalPositionUrl") or (f"https://{c['host']}{rel}" if rel else f"https://{c['host']}/careers/job/{jid}"),
                        posted=posted))


def _eightfold_board(c):
    """Whole board: an empty query lists every position and page 1 states the count (HSBC 1364 on 2026-10-11, 137
    pages), so the other pages are fetched page_workers (default 3) at a time. 78 term searches of up to 10 pages each
    cost about 800 requests and 43 to 51 HTTP 429s on HSBC; the board read is 137 requests. None when the board is
    over full_board_max."""
    first = _eightfold_page(c, "", 0)
    total = first.get("count")
    if not isinstance(total, int) or total > int(c.get("full_board_max", 1900)):
        return None
    out, seen = [], set()
    _eightfold_rows(c, first.get("positions") or [], seen, out)
    for d in _pool_map(lambda st: _eightfold_page(c, "", st), range(10, total, 10), c.get("page_workers", 3)):
        _eightfold_rows(c, d.get("positions") or [], seen, out)  # a failed page raises: caller falls back to the term search
    return out, len(seen) >= total or bool(c.get("pcsx"))  # PCSX pages overlap (Qualcomm 2082 listed, 2054 distinct), so every page read is complete


@_parallel_terms(board=_eightfold_board)
def eightfold(c):
    """Eightfold career sites (static assets on static.vscdn.net) - public /api/apply/v2/jobs endpoint.
    Needs host and domain (e.g. careers.newyorklife.com / newyorklife.com). Verified live 2026-10-02
    against New York Life (279 jobs, whole board) and HSBC (portal.careers.hsbc.com, thousands of jobs,
    so it opts into 'search' terms). The endpoint caps every page at 10 results whatever `num` says,
    so it pages with start += 10, once per search term (no 'search' key = the whole board, capped by
    max_per_term). With several terms a board under full_board_max is read whole instead (_eightfold_board).
    The list rows carry no description; describe() reads it from the per-job endpoint."""
    page = 10
    terms = _terms(c)
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 1500))
    ceiling = int(c.get("term_ceiling", 1000))
    out, seen = [], set()
    complete = True
    for term in terms:
        start, term_complete, limit = 0, False, cap
        while start < limit:
            d = _eightfold_page(c, term, start)
            total = d.get("count")
            if start == 0 and isinstance(total, int):
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            rows = d.get("positions") or []
            _eightfold_rows(c, rows, seen, out)
            start += page
            if len(rows) < page or (total is not None and start >= total):
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


def _jsonld_description(url):
    """Description from a job page's schema.org JobPosting JSON-LD block (used by TalentBrew)."""
    raw = http(url, raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("description"):
            return strip_html(data["description"]) + _jsonld_pay(raw)
    return ""


def talentbrew(c):
    """Radancy TalentBrew career sites (job URLs like /en/job/<city>/<title>/<site id>/<job id>, search
    at /search-jobs). Needs host. Verified live 2026-10-02 against Moody's. The site's own AJAX
    endpoint /search-jobs/results returns JSON whose "results" field is the job-card HTML (title,
    location, MM/DD/YYYY posted, data-job-id) 50 per page with data-total-pages; the description
    comes from each job page's JSON-LD via describe()."""
    out, seen = [], set()
    total_pages = 1
    page = 1
    while page <= min(total_pages, int(c.get("max_pages", 40))):
        q = urllib.parse.urlencode({
            "ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 50, "Distance": 50, "RadiusUnitType": 0,
            "Keywords": "", "Location": "", "ShowRadius": "False", "IsPagination": "False", "FacetType": 0,
            "SearchResultsModuleName": "Search Results", "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": 0, "SortDirection": 0, "SearchType": 5, "ResultsType": 0})
        d = http(f"https://{c['host']}/search-jobs/results?{q}", headers={"X-Requested-With": "XMLHttpRequest"}, timeout=40)
        h = d.get("results", "")
        m = re.search(r'data-total-pages="(\d+)"', h)
        if m:
            total_pages = int(m.group(1))
        for card in re.finditer(r'<a href="(/[^"]+/job/[^"]+)" data-job-id="(\d+)">(.*?)</a>', h, re.S):
            jid = card.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            body = card.group(3)
            title = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S)
            loc = re.search(r'class="job-location">(.*?)</span>', body, re.S)
            posted = re.search(r'class="job-date-posted">(\d{2})/(\d{2})/(\d{4})', body)
            out.append(dict(id=jid, title=strip_html(title.group(1)) if title else "",
                            location=strip_html(loc.group(1)) if loc else "",
                            url=f"https://{c['host']}{card.group(1)}",
                            posted=f"{posted.group(3)}-{posted.group(1)}-{posted.group(2)}" if posted else ""))
        page += 1
    return out, page > total_pages


def _avature_results_page(c, term_path, off):
    return http(f"https://{c['host']}/{c['list_path']}/{term_path}?jobOffset={off}", raw=True, timeout=40,
                headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")


def _avature_result_cards(raw, seen, out):
    """Appends the Deloitte-style cards of one list page to out (new ids only); returns the page's card count."""
    arts = raw.split('class="article--result')[1:]
    for a in arts:
        m = re.search(r'<a href="([^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        spans = [strip_html(x) for x in re.findall(r'<span>\s*(.*?)\s*</span>', a, re.S)]
        out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=html.unescape(m.group(1)),
                        location=spans[-1] if spans else "", posted=""))
    return len(arts)


def _avature_board(c):
    """Whole board of a Deloitte-style tenant: the blank search lists every job (about 1400 on 2026-10-11, 140 pages
    of 10, while the page only says "999+"). Every page up to full_board_max is queued on page_workers (default 12)
    threads and the queue stops at the first short page, so one stalled request (they hang for 30s now and then)
    never holds a whole round back. None when the tenant is another variant or the board is over full_board_max."""
    if c.get("variant") != "results":
        return None
    page, limit = int(c.get("page_size", 10)), int(c.get("full_board_max", 1900))
    end = []  # offsets of short pages seen so far; any page after the first of them is past the board

    def get(off):
        if end and off > min(end):
            return ""
        raw = _avature_results_page(c, "", off)
        if raw.count('class="article--result') < page:
            end.append(off)
        return raw

    out, seen = [], set()
    # a failed page raises, so the caller falls back to the term search
    for raw in _pool_map(get, range(0, limit, page), c.get("page_workers", 12)):
        if _avature_result_cards(raw, seen, out) < page:
            return out, True
    return None


@_parallel_terms(board=_avature_board)
def avature(c):
    """Avature career sites with a server-rendered list page. Needs host and list_path (the tenant's
    job-list page, e.g. "careers/OpenRoles"; it differs per tenant). Verified live 2026-10-02 against
    Two Sigma. 10 results per page, paged with jobOffset; each result is an <article class="article
    article--result"> holding the JobDetail link (title), then one <span class="paragraph_inner-span">
    per field, the first being the location. Descriptions come from each JobDetail page's JSON-LD."""
    out, seen = [], set()
    offset, page = 0, int(c.get("page_size", 10))
    cap = int(c.get("max_jobs", 1000))
    dates = {}
    if c.get("feed"):
        # Two Sigma's RSS feed carries real pubDates but only its 20 oldest roles (paging params are ignored),
        # so newer jobs keep a blank `posted` here; describe() finds theirs with a quoted-title feed search (_avature_feed_date).
        try:
            for it in ET.fromstring(http(f"https://{c['host']}/{c['list_path']}/feed/", raw=True, timeout=40,
                                         headers={"Accept": "application/rss+xml, */*"})).findall("channel/item"):
                try:
                    dates[(it.findtext("link", "") or "").rstrip("/").rsplit("/", 1)[-1]] = parsedate_to_datetime(
                        it.findtext("pubDate", "")).date().isoformat()
                except (TypeError, ValueError):
                    pass
        except Exception:  # noqa: BLE001  the feed is a bonus; the list pages are the source of truth
            pass
    if c.get("variant") == "results":
        # Deloitte-style: <article class="article--result "> cards (title link, then Org | Entity | Location spans),
        # 10 per page, about 1000 jobs firm-wide, so each search term is paged separately (verified live 2026-10-11).
        complete = True
        for term in _terms(c):
            for off in range(0, int(c.get("max_per_term", 200)), page):
                try:
                    raw = _avature_results_page(c, urllib.parse.quote(term), off)
                except Exception:  # noqa: BLE001
                    complete = False
                    break
                if _avature_result_cards(raw, seen, out) < page:
                    break
        return out, complete

    def get(off):
        return http(f"https://{c['host']}/{c['list_path']}?jobOffset={off}", raw=True, timeout=40,
                    headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")

    pages = {}  # offset -> html already fetched
    if c.get("variant") == "details":
        # Macquarie states its total on page 1 (591 results, 9 per page: 66 pages took 140s one after another), so
        # the other pages are independent: fetch them side by side (page_workers, default 4), then parse in order.
        pages[0] = get(0)
        total = re.search(r'(\d+) results"', pages[0])
        if total:
            offs = list(range(page, min(int(total.group(1)), cap), page))
            pages.update(zip(offs, _pool_map(get, offs, c.get("page_workers", 4))))
    while offset < cap:
        raw = pages.pop(offset, None) or get(offset)
        arts = raw.split('class="article article--result')[1:]
        for a in arts:
            if c.get("variant") == "details":
                # Macquarie-style cards: JobDetail?jobId=N link, then icon rows for ID, location, "dd Mon yyyy" posted date
                m = re.search(r'<a class="link" href="([^"]+JobDetail\?jobId=(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
                if not m or m.group(2) in seen:
                    continue
                seen.add(m.group(2))
                loc = re.search(r'alt="Office Location:"[^>]*>\s*<p>\s*(.*?)\s*</p>', a, re.S)
                pd = re.search(r'alt="Posted Date:"[^>]*>\s*<p>\s*(.*?)\s*</p>', a, re.S)
                posted = ""
                try:
                    posted = datetime.strptime(strip_html(pd.group(1)), "%d %b %Y").date().isoformat() if pd else ""
                except ValueError:
                    pass
                out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=html.unescape(m.group(1)),
                                location=strip_html(loc.group(1)) if loc else "", posted=posted))
                continue
            m = re.search(r'<a class="link" href="([^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            spans = re.findall(r'<span class="paragraph_inner-span">\s*(.*?)\s*</span>', a, re.S)
            # Bloomberg-style cards have no paragraph spans; the location sits in <span class="list-item-location"> (verified 2026-10-11)
            alt = re.search(r'class="list-item-location">\s*(.*?)\s*</span>', a, re.S)
            out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=html.unescape(m.group(1)),
                            location=strip_html(spans[0]) if spans else strip_html(alt.group(1)) if alt else "",
                            posted=dates.get(m.group(2), "")))
        offset += page
        if len(arts) < page:
            return out, True
    return out, False


def _avature_feed_date(c, job):
    """Posting date of one job from the tenant's RSS feed, "" if absent. The feed ignores paging and sorting and
    only ever returns its 20 oldest items, but its `search` filter is honored, so a quoted exact title narrows it
    to the job itself (verified live 2026-10-11 on Two Sigma: 40 of 40 undated jobs found, pubDate = posting day;
    Bloomberg's /careers/SearchJobs/feed/ behaves the same, 40 of 40 found and dates rise with job id)."""
    title = re.sub(r"[^A-Za-z0-9 ]", " ", job.get("title") or "").strip()
    if not title:
        return ""
    url = f"https://{c['host']}/{c['list_path']}/feed/?search={urllib.parse.quote(chr(34) + title + chr(34))}"
    try:
        root = ET.fromstring(http(url, raw=True, timeout=40, headers={"Accept": "application/rss+xml, */*"}))
    except Exception:  # noqa: BLE001  a bonus source: the missing date just stays blank
        return ""
    for it in root.findall("channel/item"):
        if (it.findtext("link", "") or "").rstrip("/").rsplit("/", 1)[-1] == str(job["id"]):
            try:
                return parsedate_to_datetime(it.findtext("pubDate", "")).date().isoformat()
            except (TypeError, ValueError):
                return ""
    return ""


def _kpmg_cards(raw, seen, out):
    """Appends the job cards of one KPMG list page to out (new ids only); returns (cards on the page, new ones)."""
    cards = raw.split('<a href="/jobdetail/?jobId=')[1:]
    new = 0
    for a in cards:
        jid = re.match(r"(\d+)", a)
        if not jid or jid.group(1) in seen:
            continue
        seen.add(jid.group(1))
        new += 1
        t = re.search(r'<div class="h5 text-dark-grey">\s*(.*?)\s*</div>', a, re.S)
        l = re.search(r'<div class="text-xs text-dark-grey">\s*([^<]*?\|[^<]*?)\s*</div>', a, re.S)
        out.append(dict(id=jid.group(1), title=strip_html(t.group(1)) if t else "",
                        url=f"https://www.kpmguscareers.com/jobdetail/?jobId={jid.group(1)}",
                        location=strip_html(l.group(1)).split("|", 1)[-1].strip() if l else "", posted=""))
    return len(cards), new


def _kpmg_page(term, pg):
    return http(f"https://www.kpmguscareers.com/job-search/?keyword={urllib.parse.quote(term)}&spage={pg}",
                raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")


def _kpmg_board(c):
    """Whole KPMG board: an empty keyword lists every job (870 on 2026-10-11, 73 pages of 12) and page 1 states the
    total in <span data-action="count">, so the other pages are fetched side by side (page_workers, default 4)."""
    first = _kpmg_page("", 1)
    m = re.search(r'data-action="count">(\d+)<', first)
    if not m or int(m.group(1)) > int(c.get("full_board_max", 1900)):
        return None
    out, seen = [], set()
    n_pages = -(-int(m.group(1)) // 12)
    # The list repeats some jobs (870 cards, 786 distinct) and now and then one read moves a repeat across a page
    # boundary and drops a distinct job (1 read in 6), so the board is read board_passes (default 2) times and merged.
    for p in range(int(c.get("board_passes", 2))):
        pages = [first] if p == 0 else []
        # a failed page raises, so the caller falls back to the term search rather than returning a partial board
        pages += _pool_map(lambda pg: _kpmg_page("", pg), range(1 if p else 2, n_pages + 1), c.get("page_workers", 4))
        for raw in pages:
            _kpmg_cards(raw, seen, out)
    return out, True


@_parallel_terms(board=_kpmg_board)
def kpmg(c):
    """KPMG US careers (WordPress, server-rendered). /job-search/?keyword=<term>&spage=<n> returns 12 cards per
    page; each card links /jobdetail/?jobId=N and carries a "Practice | Location" line. Verified live
    2026-10-11. `posted` stays blank (cards show no date). With several terms the whole board is read first
    (_kpmg_board) and the term search is only the fallback for a board over full_board_max."""
    out, seen, complete = [], set(), True
    for term in _terms(c):
        for pg in range(1, int(c.get("max_pages", 12)) + 1):
            try:
                raw = _kpmg_page(term, pg)
            except Exception:  # noqa: BLE001
                complete = False
                break
            n_cards, new = _kpmg_cards(raw, seen, out)
            if n_cards < 12 or not new:
                break
    return out, complete


def _adp_rm_headers(dom):
    """Headers for the ADP RM job-requisitions API: the career-site endpoint hands out a short-lived myJobsToken."""
    hdr = {"User-Agent": UA["User-Agent"], "Accept": "application/json"}
    site = http(f"https://myjobs.adp.com/public/staffing/v1/career-site/{dom}", headers=hdr)
    return dict(hdr, myjobstoken=site["myJobsToken"], rolecode="manager", Referer="https://myjobs.adp.com/")


def _adp_rm_detail(c, job_id):
    """(description html, posted) from the per-requisition search-meta endpoint (the one the job page itself calls).
    The list's postingDate is always empty, but this carries postingInstructions[].timestampLastPosted, the date the
    job page's "posted" age is computed from; it moves if the requisition is re-posted."""
    d = http(f"https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/search-meta/{job_id}?tz=America/New_York",
             headers=_adp_rm_headers(c["domain"]))
    req = (d.get("jobRequisitions") or [{}])[0]
    stamps = [str(p.get("timestampLastPosted") or "")[:10] for p in req.get("postingInstructions") or []]
    posted = max((x for x in stamps if re.match(r"\d{4}-\d\d-\d\d$", x)), default="")
    return req.get("requisitionDescription") or "", posted


def adp_rm(c):
    """ADP Recruiting Management career sites (myjobs.adp.com/<domain>/cx). The career-site endpoint hands out a
    short-lived myJobsToken that the job-requisitions API needs as a header (plus rolecode and Referer); the list call
    already carries the description but its postingDate is empty, so describe() reads the real posting date from the
    requisition's search-meta endpoint (_adp_rm_detail). Needs `domain` (e.g. "vnbcareers"). Verified live 2026-10-11
    against Valley National (187 jobs) and BankUnited (96 jobs)."""
    dom = c["domain"]
    h2 = _adp_rm_headers(dom)
    out, seen, skip, top = [], set(), 0, 50
    sel = "reqId,jobTitle,publishedJobTitle,jobDescription,jobQualifications,postingDate,requisitionLocations"
    while skip < int(c.get("max_jobs", 1500)):
        d = http("https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters"
                 f"?$orderby=postingDate%20desc&$select={sel}&$top={top}&$skip={skip}&tz=America/New_York", headers=h2)
        rows = d.get("jobRequisitions") or []
        for r in rows:
            rid = str(r.get("reqId"))
            if rid in seen:
                continue
            seen.add(rid)
            locs = []
            for l in r.get("requisitionLocations") or []:
                a = l.get("address") or {}
                parts = [a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue"), (a.get("country") or {}).get("codeValue")]
                locs.append(", ".join(p for p in parts if p))
            out.append(dict(id=rid, title=r.get("publishedJobTitle") or r.get("jobTitle") or "",
                            url=f"https://myjobs.adp.com/{dom}/cx/job-details?reqId={rid}", location=" | ".join(locs),
                            posted=(r.get("postingDate") or "")[:10],
                            desc=strip_html((r.get("jobDescription") or "") + " " + (r.get("jobQualifications") or ""))))
        if len(rows) < top:
            return out, True
        skip += top
    return out, False


def adp_wfn(c):
    """ADP Workforce Now recruitment sites (workforcenow.adp.com/mascsr/.../recruitment.html?cid=...). The public
    job-requisitions API needs no token: list pages ($top/$skip) carry title, postDate and locations, and the
    description comes from the per-requisition detail call in describe(). Needs `cid` (from the careers page's ADP
    link) and optional ccId. Verified live 2026-10-11 against Prosperity Bank (212 jobs) and Hanmi (14)."""
    cid, cc = c["cid"], c.get("ccId", "19000101_000001")
    base = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    hdr = {"User-Agent": UA["User-Agent"], "Accept": "application/json"}
    out, seen, skip, top = [], set(), 0, 20
    while skip < int(c.get("max_jobs", 1500)):
        d = http(f"{base}?cid={cid}&ccId={cc}&lang=en_US&locale=en_US&$top={top}&$skip={skip}", headers=hdr)
        rows = d.get("jobRequisitions") or []
        for r in rows:
            rid = str(r.get("itemID"))
            if rid in seen:
                continue
            seen.add(rid)
            locs = [((l.get("nameCode") or {}).get("shortName") or "") for l in r.get("requisitionLocations") or []]
            out.append(dict(id=rid, title=r.get("requisitionTitle", ""),
                            url=f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={cc}&jobId={r.get('clientRequisitionID')}&lang=en_US",
                            location=" | ".join(x for x in locs if x), posted=(r.get("postDate") or "")[:10]))
        if not rows:
            return out, True
        skip += len(rows)
    return out, False


def ukg(c):
    """UKG Pro Recruiting job boards (<tenant>.rec.pro.ukg.net/<code>/JobBoard/<uuid>). POST JobBoardView/LoadSearchResults
    pages {Top, Skip} and returns title, locations, PostedDate and a BriefDescription (all describe() gets; the full text
    sits behind the JS-rendered detail page). Needs `board` (the JobBoard URL without trailing slash). Verified live
    2026-10-11 against Cathay Bank (112 jobs)."""
    board = c["board"].rstrip("/")
    out, seen, skip, top = [], set(), 0, 50
    while skip < int(c.get("max_jobs", 1500)):
        body = {"opportunitySearch": {"Top": top, "Skip": skip, "QueryString": "",
                                      "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                      "Filters": []},
                "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [],
                                  "hasNoLicenses": False, "SkippedSkills": []}}
        d = http(f"{board}/JobBoardView/LoadSearchResults", data=body, headers={"Accept": "application/json"}, timeout=40)
        rows = d.get("opportunities") or []
        for r in rows:
            rid = str(r.get("Id"))
            if rid in seen:
                continue
            seen.add(rid)
            locs = []
            for l in r.get("Locations") or []:
                a = l.get("Address") or {}
                locs.append(", ".join(p for p in (a.get("City"), (a.get("State") or {}).get("Code"), (a.get("Country") or {}).get("Code")) if p))
            out.append(dict(id=rid, title=r.get("Title", ""), url=f"{board}/OpportunityDetail?opportunityId={rid}",
                            location=" | ".join(x for x in locs if x), posted=(r.get("PostedDate") or "")[:10],
                            desc=strip_html(r.get("BriefDescription") or "")))
        if not rows:
            return out, True
        skip += len(rows)
    return out, False


def peoplesoft_hrs(c):
    """Oracle PeopleSoft HCM Candidate Gateway (Fluid) career sites, e.g. careers.federatedhermes.com. A session
    GET of <host>/psc/EXT/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL?FOCUS=Applicant&Page=HRS_APP_SCHJOB&SiteId=<n>
    yields ICSID/ICStateNum form state; a form POST with ICAction=HRS_SCH_WRK_FLU_HRS_SEARCH_BTN returns the first
    50 rows as server-rendered HTML, and ICAction=HRS_AGNT_RSLT_I$hdown$0 on that state returns the rest. Rows carry
    Job Title, Job ID, Location (city only), Department, Job Family and Posted Date (mm/dd/yyyy). The detail URL
    (Page=HRS_APP_JBPST_FL&JobOpeningId=<id>&PostingSeq=1) works in a fresh cookie session. Needs host; optional
    site (default 1), path. Verified live 2026-10-11 against Federated Hermes (65 jobs)."""
    base = "https://{}/{}".format(c["host"], c.get("path", "psc/EXT/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL").strip("/"))
    site = c.get("site", 1)
    ua = {"User-Agent": BROWSER_UA}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def get(url, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        return opener.open(urllib.request.Request(url, data=data, headers=ua), timeout=40).read().decode("utf-8", "ignore")

    def state(page):
        sid = re.search(r"name='ICSID'[^>]*value='([^']*)'", page)
        num = re.search(r"name='ICStateNum'[^>]*value='(\d+)'", page)
        return (sid.group(1), num.group(1)) if sid and num else None

    try:
        page = get(f"{base}?FOCUS=Applicant&Page=HRS_APP_SCHJOB&SiteId={site}")
        st = state(page)
        if not st:
            return [], False
        page = get(base, {"ICStateNum": st[1], "ICAction": "HRS_SCH_WRK_FLU_HRS_SEARCH_BTN", "ICSID": st[0], "HRS_SCH_WRK_HRS_SCH_TEXT100": ""})
        total = re.search(r">\s*(\d+)\s*</span>\s*(?:<[^>]+>\s*)*jobs? found", page, re.I) or re.search(r"(\d+)\s*(?:<[^>]+>\s*)*jobs found", page, re.I)
        total = int(total.group(1)) if total else None
        if total and len(re.findall(r"id='SCH_JOB_TITLE\$\d+'", page)) < total and state(page):
            st = state(page)
            page = get(base, {"ICStateNum": st[1], "ICAction": "HRS_AGNT_RSLT_I$hdown$0", "ICSID": st[0]})
    except Exception:  # noqa: BLE001
        return [], False
    out, seen = [], set()
    for chunk in re.split(r"win0divSCH_JOB_TITLE\$\d+'", page)[1:]:
        f = {k.strip(): html.unescape(v).strip() for k, v in
             re.findall(r"class='ps-label'>([^<]+)</span></DIV><span class='ps_box-value'[^>]*>([^<]*)</span>", chunk[:4000])}
        jid, title = f.get("Job ID"), f.get("Job Title")
        if not jid or not title or jid in seen:
            continue
        seen.add(jid)
        pd = re.match(r"(\d+)/(\d+)/(\d{4})", f.get("Posted Date", ""))
        out.append(dict(id=jid, title=title, location=f.get("Location", ""),
                        url=f"{base}?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId={site}&JobOpeningId={jid}&PostingSeq=1",
                        posted=f"{pd.group(3)}-{int(pd.group(1)):02d}-{int(pd.group(2)):02d}" if pd else ""))
    return out, bool(out) and (total is None or len(out) >= total)


def csod(c):
    """Cornerstone OnDemand career sites (<corp>.csod.com/ux/ats/careersite/<site>/home?c=<corp>). The home page embeds
    a public bearer token and the API cloud ("token", "cloud" in its inline JSON); POST <cloud>rec-job-search/external/jobs
    with it returns requisitions (id, displayJobTitle, locations[{city,state,country}], postingEffectiveDate m/d/yyyy)
    in pages of pageNumber/pageSize. Needs corp; optional site (default 1). Verified live 2026-10-11 against
    FHLB Cincinnati (fhlbcin, 6 jobs, all Cincinnati OH)."""
    corp, site = c["corp"], int(c.get("site", 1))
    base = f"https://{corp}.csod.com"
    page = http(f"{base}/ux/ats/careersite/{site}/home?c={corp}", raw=True, headers={"User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
    tok, cloud = re.search(r'"token"\s*:\s*"([^"]+)"', page), re.search(r'"cloud"\s*:\s*"([^"]+)"', page)
    if not tok or not cloud:
        return [], False
    hdr = {"Authorization": f"Bearer {tok.group(1)}", "User-Agent": BROWSER_UA}
    out, seen, pg, total = [], set(), 1, None
    while pg <= int(c.get("max_pages", 40)):
        body = dict(careerSiteId=site, careerSitePageId=site, pageNumber=pg, pageSize=25, cultureId=1, searchText="", cultureName="en-US",
                    states=[], countryCodes=[], cities=[], placeID="", radius=None, postingsWithinDays=None,
                    customFieldCheckboxKeys=[], customFieldDropdowns=[], customFieldRadios=[])
        try:
            d = (http(cloud.group(1).rstrip("/") + "/rec-job-search/external/jobs", data=body, headers=hdr) or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            return out, False
        total = d.get("totalCount", total)
        reqs = d.get("requisitions") or []
        for r in reqs:
            rid = str(r.get("requisitionId"))
            if rid in seen:
                continue
            seen.add(rid)
            locs = ["{}, {}, {}".format(l.get("city") or "", l.get("state") or "", l.get("country") or "").replace(" ,", "").strip(" ,")
                    for l in r.get("locations") or []]
            pd = re.match(r"(\d+)/(\d+)/(\d{4})", r.get("postingEffectiveDate") or "")
            out.append(dict(id=rid, title=(r.get("displayJobTitle") or "").strip(), location="; ".join(dict.fromkeys(locs)),
                            url=f"{base}/ux/ats/careersite/{site}/home/requisition/{rid}?c={corp}",
                            posted=f"{pd.group(3)}-{int(pd.group(1)):02d}-{int(pd.group(2)):02d}" if pd else "",
                            desc=strip_html(r.get("externalDescription") or "")))
        if not reqs or (total is not None and len(seen) >= total):
            return out, True
        pg += 1
    return out, False


def taleo_cws(c):
    """Oracle Taleo Business Edition Career Center v2 (CWS) boards on <host>/<path>/ats/careers/v2, e.g.
    phh.tbe.taleo.net/phh01 for FHLBank Boston. A plain GET of searchResults?org=<org>&cws=<n> lists every open
    requisition on one page (no session cookie needed): accordion rows with a viewJobLink title anchor (rid in the
    URL) and the location in the next div. No posted date in the list. Needs host, path, org, cws. Verified live
    2026-10-11 against FHLBank Boston (4 jobs)."""
    base = f"https://{c['host']}/{c['path'].strip('/')}/ats/careers/v2"
    page = http(f"{base}/searchResults?org={c['org']}&cws={c['cws']}", raw=True, headers={"User-Agent": BROWSER_UA})
    page = page.decode("utf-8", "ignore") if isinstance(page, bytes) else page
    out, seen = [], set()
    for m in re.finditer(r'<h4 class="[^"]*head-title[^"]*"><a href="([^"]+)" class="viewJobLink">(.*?)</a></h4>\s*<div[^>]*>(.*?)</div>', page, re.S):
        url = html.unescape(m.group(1))
        rid = re.search(r"[?&]rid=(\d+)", url)
        if not rid or rid.group(1) in seen:
            continue
        seen.add(rid.group(1))
        out.append(dict(id=rid.group(1), title=strip_html(m.group(2)), url=url, location=strip_html(m.group(3)), posted=""))
    return out, "oracletaleocwsv2" in page  # the Career Center chrome is present, so an empty list is a real empty board


def taleo_be(c):
    """Oracle Taleo Business Edition career sites (TVAPP pages such as careers.zionsbancorp.com, job URLs
    /jobs/<slug>-<id>). The list is a saved search: POST /ajax/content/job_results?JobSearch.id=<id>&page_index=N
    returns JSON whose Result is the HTML of 10 job rows (title link, location, category). The AJAX layer rejects
    requests without a session cookie and the page's hidden #tsstoken sent as header tss-token, so one GET of the
    search page comes first. Needs host, search_id, site_name (TVAPP.property.site.short_name in the page source).
    No posted date is published. Verified live 2026-10-11 against Zions Bancorporation (101 jobs)."""
    base = f"https://{c['host']}"
    ua = {"User-Agent": BROWSER_UA}  # the site answers 403 to non-browser user agents
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    page = opener.open(urllib.request.Request(f"{base}/jobs/search/{c['search_id']}", headers=ua), timeout=40).read().decode("utf-8", "ignore")
    m = re.search(r'id\s*=\s*"tsstoken"\s*value\s*=\s*"([^"]+)"', page)
    if not m:
        return [], False
    hdr = dict(ua, **{"tss-token": m.group(1), "X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/jobs/search/{c['search_id']}"})
    out, seen = [], set()
    max_pages, pg = int(c.get("max_pages", 60)), 0
    while pg < max_pages:
        pg += 1
        url = f"{base}/ajax/content/job_results?JobSearch.id={c['search_id']}&page_index={pg}&site-name={c['site_name']}&include_site=true"
        try:
            res = json.loads(opener.open(urllib.request.Request(url, data=b"", headers=hdr), timeout=40).read())
        except Exception:  # noqa: BLE001
            return out, False
        html_rows = res.get("Result") or ""
        rows = re.split(r'<div id="job_list_(\d+)"', html_rows)[1:]
        if not rows:
            return out, True
        total = re.search(r'id="jPaginateNumPages"[^>]*>(\d+)', html_rows)  # past the last page the server errors instead of returning nothing
        max_pages = min(max_pages, int(total.group(1))) if total else max_pages
        for jid, body in zip(rows[0::2], rows[1::2]):
            lm = re.search(r'<a href="([^"]+)" class="job_link[^"]*">(.*?)</a>', body, re.S)
            if not lm or jid in seen:
                continue
            seen.add(jid)
            loc = re.search(r'<span class="location">(.*?)</span>', body, re.S)
            out.append(dict(id=jid, title=strip_html(lm.group(2)), url=html.unescape(lm.group(1)),
                            location=strip_html(loc.group(1)) if loc else "", posted=""))
    return out, pg >= max_pages and bool(total)


def talentsoft(c):
    """Cegid Talentsoft career sites (e.g. jobs.ca-cib.com, job URLs /job/<slug>_<id>.aspx). Server-rendered list at
    <list_path>?LCID=2057&page=N (100 offer cards per page, each with contract, country and city items). Needs host
    and optional list_path (default /offre-de-emploi/liste-offres.aspx). No posted date is published (the page only
    shows a "Date de mise a jour" that varies per job and is never earlier than the posting), so describe() reads
    that from the detail page as an "approx" date. Verified live 2026-10-11 against Credit Agricole CIB (332 jobs)."""
    base = f"https://{c['host']}"
    path = c.get("list_path", "/offre-de-emploi/liste-offres.aspx")
    ua = {"User-Agent": BROWSER_UA, "Accept": "text/html, */*"}
    out, seen = [], set()
    for pg in range(1, int(c.get("max_pages", 30)) + 1):
        raw = http(f"{base}{path}?LCID=2057&page={pg}", raw=True, timeout=40, headers=ua).decode("utf-8", "ignore")
        cards = raw.split('class="ts-offer-card Layer"')[1:]
        fresh = 0
        for card in cards:
            m = re.search(r'ts-offer-card__title-link[^>]*href="([^"]+_(\d+)\.aspx)[^>]*>(.*?)</a>', card, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            fresh += 1
            items = [strip_html(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", card, re.S)]
            loc = ", ".join(x for x in reversed(items[1:3]) if x)  # items are contract, country, city
            out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=base + html.unescape(m.group(1)), location=loc, posted=""))
        if not fresh:  # past the last page the site repeats or empties the list
            return out, True
    return out, False


def bpce(c):
    """BPCE group WordPress career sites (recrutement.natixis.com). One POST to /app/wp-json/bpce/v1/search/jobs
    with a large `size` returns every job with its full HTML description, posting date and city, so there is no
    paging and describe() has nothing to fetch. Needs host. Country is "International" for non-French sites, in
    which case the region field holds the real country (e.g. "United States"). Applications go to a shared Oracle
    site whose public list is a stale subset, which is why the oracle fetcher is not used. Verified live
    2026-10-11 against Natixis (174 jobs, 16 in New York)."""
    base = f"https://{c['host']}"
    body = {"lang": "en", "keyword": "", "tax_sector": "", "tax_contract": "", "tax_place": "", "tax_job": "", "tax_experience": "",
            "tax_degree": "", "tax_brands": "", "tax_department": "", "tax_city": "", "tax_country": "", "tax_channel": "",
            "jobcode": "", "tax_community_job": "", "external": False, "userID": "", "from": 0, "size": int(c.get("max_jobs", 1000))}
    d = (http(f"{base}/app/wp-json/bpce/v1/search/jobs", data=body, timeout=60, headers={"User-Agent": BROWSER_UA}) or {}).get("data") or {}
    items = d.get("items") or []
    out = []
    for it in items:
        locs = []
        for l in it.get("localisations") or []:
            country = l.get("region") if l.get("country") == "International" else l.get("country")
            locs.append(", ".join(x for x in (l.get("city"), country) if x))
        out.append(dict(id=str(it.get("job_number") or it.get("post_id")), title=strip_html(it.get("title", "")),
                        url=base + ((it.get("link") or {}).get("url") or ""), location=" | ".join(locs),
                        posted=(it.get("date") or "")[:10], desc=strip_html(it.get("description", ""))))
    return out, bool(items) and len(items) >= int(d.get("total") or 0)


def paylocity(c):
    """Paylocity Recruiting job boards (recruiting.paylocity.com/recruiting/jobs/All/<guid>/<name>). The board page
    embeds every job in window.pageData ("Jobs": id, title, city/state, ISO PublishedDate, 110-char snippet), so one
    GET lists the lot with real posting dates; describe() reads the detail page's JSON-LD. Needs `board` (the board
    URL). Verified live 2026-10-11 against ConnectOne Bank (36 jobs)."""
    raw = http(c["board"], raw=True, timeout=40, headers={"Accept": "text/html, */*", "User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
    m = re.search(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", raw, re.S)
    if not m:
        return [], False
    out = []
    for j in json.loads(m.group(1)).get("Jobs") or []:
        loc = j.get("JobLocation") or {}
        place = ", ".join(x for x in (loc.get("City") or j.get("LocationName"), loc.get("State")) if x)
        out.append(dict(id=str(j["JobId"]), title=j.get("JobTitle", ""), location=place,
                        url=f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{j['JobId']}",
                        posted=(j.get("PublishedDate") or "")[:10]))
    return out, True


def deshaw(c):
    """D. E. Shaw's own careers site (Next.js, server-rendered). The job list is the set of
    /careers/<slug>-<id> links on /careers/choose-your-path; each job page embeds the full job as
    __NEXT_DATA__ JSON (jobData: displayName, jobLocations, description fields), so one page fetch per
    job gives title, location and description. Verified live 2026-10-02 (about 95 jobs). The
    "all-positions-in-..." slugs are category landing pages, not jobs, and are skipped. datePosted on
    the page is just the render time, so `posted` stays blank."""
    raw = http("https://www.deshaw.com/careers/choose-your-path", raw=True, timeout=40,
               headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    paths = sorted(set(re.findall(r'href="(/careers/(?!all-positions)[a-z0-9-]+-\d{4})"', raw)))

    def one(path):
        page = http("https://www.deshaw.com" + path, raw=True, timeout=40,
                    headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        jd = json.loads(m.group(1))["props"]["pageProps"]["jobData"]
        desc = jd.get("jobDescription") or {}
        text = " ".join(strip_html(x or "") for x in (desc.get("websiteDescription"), desc.get("responsibilitiesHtml"),
                                                       desc.get("peopleWeAreLookingForStr")))
        locs = [x.get("name", "") for x in (jd.get("jobMetadata") or {}).get("jobLocations") or []]
        return dict(id=str(jd["id"]), title=jd.get("displayName", ""), location=" | ".join(x for x in locs if x),
                    url="https://www.deshaw.com" + path, posted="", desc=text)

    out, complete = [], True
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(lambda x: _try(one, x), paths):
            if r is None:
                complete = False  # a page failed: don't let a partial list close queued postings
            else:
                out.append(r)
    return out, complete


def _try(fn, arg):
    try:
        return fn(arg)
    except Exception:  # noqa: BLE001
        return None


def brassring(c):
    """Kenexa/IBM BrassRing "TGnewUI" career sites. Needs host, partnerid, siteid. Verified live
    2026-10-02 against UBS (jobs.ubs.com, about 530 jobs). The AJAX search needs a browser-like session,
    reverse-engineered from the page: (1) GET the search page with a cookie jar, which sets tg_session
    cookies; (2) the cookie tg_session_<partner>_<site> is the `encryptedsessionvalue` for every call;
    (3) every POST also needs header RFT = the hidden __RequestVerificationToken input (apply.js reads
    it the same way) plus Referer/Origin; (4) POST /Search/Ajax/MatchedJobs returns page 1 (50 jobs),
    POST /Search/Ajax/ProcessSortAndShowMoreJobs with pageNumber 2.. returns the rest. Each job is a
    Questions list of name/value pairs; the list call already includes the full jobdescription, so
    describe() needs no brassring branch. Location is formtext23 ("United States - New York")."""
    import http.cookiejar
    base = f"https://{c['host']}"
    pid, sid = str(c["partnerid"]), str(c["siteid"])
    ref = (f"{base}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={pid}&siteid={sid}"
           f"&PageType=searchResults&SearchType=linkquery&LinkID={c.get('linkid', '')}")
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    page = opener.open(urllib.request.Request(ref, headers=ua), timeout=60).read().decode("utf-8", "ignore")
    tok = next(ck.value for ck in cj if ck.name == f"tg_session_{pid}_{sid}")
    rft = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page).group(1)

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
            **ua, "Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01", "Referer": ref, "Origin": base, "RFT": rft})
        return json.loads(opener.open(req, timeout=60).read())

    kw, loc = "FORMTEXT2,FORMTEXT21,AutoReq,Department,JobTitle", "FORMTEXT2,FORMTEXT23,Location"
    first = post("/TgNewUI/Search/Ajax/MatchedJobs", {
        "PartnerId": pid, "SiteId": sid, "Keyword": "", "Location": "", "KeywordCustomSolrFields": kw,
        "LocationCustomSolrFields": loc, "FacetFilterFields": None, "TurnOffHttps": False, "Latitude": 0,
        "Longitude": 0, "PowerSearchOptions": {"PowerSearchOption": []}, "encryptedsessionvalue": tok})
    total = int(first.get("JobsCount") or first.get("TotalJobsCount") or 0)
    rows = list((first.get("Jobs") or {}).get("Job") or [])
    page_no = 2
    while len(rows) < total and page_no <= int(c.get("max_pages", 40)):
        d = post("/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs", {
            "partnerId": pid, "siteId": sid, "keyword": "", "location": "", "keywordCustomSolrFields": kw,
            "locationCustomSolrFields": loc, "linkId": "", "Latitude": 0, "Longitude": 0,
            "facetfilterfields": {"Facet": []}, "powersearchoptions": {"PowerSearchOption": []},
            "SortType": "LastUpdated", "pageNumber": page_no, "encryptedSessionValue": tok})
        got = (d.get("Jobs") or {}).get("Job") or []
        if not got:
            break
        rows.extend(got)
        page_no += 1
    out, seen = [], set()
    for j in rows:
        q = {x.get("QuestionName"): x.get("Value") for x in j.get("Questions") or []}
        jid = str(q.get("reqid") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        posted = ""
        try:
            posted = datetime.strptime(q.get("lastupdated") or "", "%d-%b-%Y").date().isoformat()
        except ValueError:
            pass
        out.append(dict(id=jid, title=strip_html(q.get("jobtitle") or ""), location=strip_html(q.get("formtext23") or ""),
                        url=j.get("Link") or ref, posted=posted, desc=strip_html(q.get("jobdescription") or ""),
                        posted_source="approx" if posted else ""))  # lastupdated is an update date: the detail page has no PostDate value
    return out, len(rows) >= total


def jibe(c):
    """iCIMS Career Sites ("Jibe") front ends - public /api/jobs?page=N&limit=100 on the careers host.
    Needs host and job_path (the site's public job URL prefix, e.g. "us/jobs"). Verified live 2026-10-02
    against Principal Financial (careers.principal.com, 139 jobs). Each job carries title, location
    fields, the full description/responsibilities/qualifications and an exact posted_date, so no
    describe() call is needed."""
    out, seen = [], set()
    page, limit = 1, 100
    while page <= int(c.get("max_pages", 30)):
        d = http(f"https://{c['host']}/api/jobs?page={page}&limit={limit}", headers={"Accept": "application/json"}, timeout=40)
        rows = d.get("jobs") or []
        for r in rows:
            j = r.get("data") or {}
            jid = str(j.get("req_id") or j.get("slug") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            loc = j.get("full_location") or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country_code")) if x)
            text = " ".join(strip_html(j.get(k) or "") for k in ("description", "responsibilities", "qualifications"))
            out.append(dict(id=jid, title=j.get("title", "") or "", location=loc or "",
                            url=f"https://{c['host']}/{c.get('job_path', 'jobs').strip('/')}/{j.get('slug') or jid}",
                            posted=str(j.get("posted_date") or j.get("create_date") or "")[:10], desc=text))
        if len(rows) < limit:
            return out, True
        page += 1
    return out, False


_TALEO_AGE_BUCKETS = {1: (0, 0), 2: (1, 1), 3: (2, 7), 4: (8, 14), 5: (15, 21), 6: (22, 28)}  # POSTING_DATE filter id -> age range in days


def _taleo_page(c, page, age_bucket=None):
    """One page of the section's searchjobs POST, optionally limited to a POSTING_DATE age bucket id."""
    empty = lambda ids: [dict(id=i, selectedValues=[]) for i in ids]
    sel = empty(["POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL"])
    if age_bucket:
        sel[0]["selectedValues"] = [str(age_bucket)]
    body = {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
            "filterSelectionParam": {"searchFilterSelections": sel},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": empty(["ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS", "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT"])},
            "pageNo": page}
    return http(f"https://{c['host']}/careersection/rest/jobboard/searchjobs?lang=en&portal={c['portal']}", data=body, timeout=40,
                headers={"Accept": "application/json", "tz": "GMT-05:00", "tzname": "America/New_York", "X-Requested-With": "XMLHttpRequest"})


def _taleo_age_dates(c):
    """{job number: approx ISO date} for sections whose list has no date column. The POSTING_DATE filter (Today,
    Yesterday, Last 7/14/21/28 Days; honored even when the section shows no such facet) buckets each job's age, so
    the bucket midpoint is its date. Jobs older than 28 days stay undated. A failed call keeps what was found."""
    out = {}
    for bucket, (lo, hi) in _TALEO_AGE_BUCKETS.items():
        mid = (date.today() - timedelta(days=(lo + hi) // 2)).isoformat()
        for page in range(1, 9):
            try:
                d = _taleo_page(c, page, bucket)
            except Exception:  # noqa: BLE001
                return out
            rows = d.get("requisitionList") or []
            for r in rows:
                out.setdefault(str(r.get("contestNo") or r.get("jobId") or ""), mid)  # buckets are cumulative: the smallest one wins
            if len(rows) < int((d.get("pagingData") or {}).get("pageSize") or 25):
                break
    return out


def taleo(c):
    """Oracle Taleo Enterprise career sections - POST /careersection/rest/jobboard/searchjobs (25 per page).
    Needs host, section and portal (the number in the section's jobsearch.ftl?portal= URL). The list
    columns are title, locations (JSON string) and an exact posted date. Verified live 2026-10-02
    against Equitable (equitable.taleo.net, section eqh_1) and 2026-10-11 against Cincinnati Financial. Sections
    that order the columns differently (Cincinnati: job number, title, locations, no date) set "columns":
    [title, location, posted] indexes, -1 for a missing one; with no date column the jobs get an "approx" date
    from the POSTING_DATE age buckets (_taleo_age_dates, verified live 2026-10-11 on Cincinnati Financial; the
    detail page carries no date either)."""
    ti, li, pi = c.get("columns", [0, 1, 2])
    host, sec = c["host"], c["section"]
    out, seen, page, complete = [], set(), 1, False
    while page <= int(c.get("max_pages", 60)):
        d = _taleo_page(c, page)
        rows = d.get("requisitionList") or []
        for r in rows:
            jid = str(r.get("contestNo") or r.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            col = r.get("column") or []
            at = lambda i: col[i] if 0 <= i < len(col) else ""
            try:
                locs = json.loads(at(li)) if at(li) else []
                loc = "; ".join(locs) if isinstance(locs, list) else str(locs)
            except ValueError:
                loc = at(li)
            posted = ""
            try:
                posted = datetime.strptime(at(pi).strip(), "%b %d, %Y").date().isoformat() if at(pi) else ""
            except ValueError:
                pass
            out.append(dict(id=jid, title=strip_html(at(ti)), location=loc,
                            url=f"https://{host}/careersection/{sec}/jobdetail.ftl?job={jid}&lang=en", posted=posted))
        if len(rows) < int((d.get("pagingData") or {}).get("pageSize") or 25):
            complete = True
            break
        page += 1
    if pi < 0:
        dates = _taleo_age_dates(c)
        for j in out:
            if dates.get(j["id"]):
                j["posted"], j["posted_source"] = dates[j["id"]], "approx"
    return out, complete


def hrmdirect(c):
    """ClearCompany HRM Direct boards (e.g. Oppenheimer, opco.hrmdirect.com) - server-rendered list at
    /employment/job-openings.php, one detail page per req for location and description. No posted date is
    published, so first_seen stands in. Verified live 2026-10-03 (51 reqs)."""
    base = f"https://{c['host']}/employment"
    raw = http(f"{base}/job-openings.php?search=true&nohd=&dept=-1&office=-1&cust_sort1=-1", raw=True, timeout=40,
               headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    out, seen = [], set()
    for req, title in re.findall(r"""<a href="job-opening\.php\?req=(\d+)[^"]*">([^<]+)</a>""", raw):
        if req in seen:
            continue
        seen.add(req)
        url = f"{base}/job-opening.php?req={req}&&nohd"
        page = http(url, raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
        text = strip_html(page)
        m = re.search(r"Location:\s*(.+?)\s+Type of Hire:", text)
        out.append(dict(id=req, title=strip_html(title), location=m.group(1) if m else "", url=url, posted="", desc=text[:30000]))
    return out, True


def commerzbank(c):
    """Commerzbank's own job board (jobs.commerzbank.com) - public POST api-jobs.commerzbank.com/search/ with
    an OData-style SearchParameters body; FirstItem is 1-based. Each item carries title, URI, locations and
    an exact PublicationStartDate. Verified live 2026-10-03 (450 jobs). The description comes from describe()."""
    out, seen, first, step = [], set(), 1, 100
    while first < int(c.get("max_jobs", 2000)):
        body = {"SearchParameters": {"FirstItem": first, "CountItem": step, "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
                                     "MatchedObjectDescriptor": ["ID", "PositionTitle", "PositionURI", "PositionLocation.CountryName", "PositionLocation.CityName", "PublicationStartDate"]},
                "SearchCriteria": [], "LanguageCode": "EN"}
        d = http("https://api-jobs.commerzbank.com/search/", data=body, timeout=40,
                 headers={"Accept": "application/json", "Origin": "https://jobs.commerzbank.com", "Referer": "https://jobs.commerzbank.com/"})
        items = (d.get("SearchResult") or {}).get("SearchResultItems") or []
        for it in items:
            m = it.get("MatchedObjectDescriptor") or {}
            jid = str(m.get("ID") or it.get("MatchedObjectId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = ["{}, {}".format(x.get("CityName", ""), x.get("CountryName", "")).strip(", ") for x in m.get("PositionLocation") or []]
            out.append(dict(id=jid, title=m.get("PositionTitle", "") or "", location="; ".join(locs), url=m.get("PositionURI") or f"https://jobs.commerzbank.com/index.php?ac=jobad&id={jid}",
                            posted=str(m.get("PublicationStartDate") or "")[:10]))
        if len(items) < step:
            return out, True
        first += step
    return out, False


def dzbank(c):
    """DZ Bank's karriere.dzbank.de publishes the whole board, descriptions included, as one JSON array at
    /bin/dzbank/sapjobs.json (SuccessFactors behind it). Verified live 2026-10-03 (56 jobs, dd.mm.yyyy dates)."""
    rows = http("https://karriere.dzbank.de/bin/dzbank/sapjobs.json", timeout=40)
    out = []
    for r in rows:
        posted = ""
        try:
            posted = datetime.strptime(r.get("publiziert") or "", "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
        out.append(dict(id=str(r.get("jobReqId")), title=r.get("stellenbezeichnung", "") or "", location=r.get("standort", "") or "",
                        url=r.get("joblink") or "", posted=posted, desc=strip_html(r.get("beschreibung") or "")))
    return [j for j in out if j["id"] and j["url"]], True


def marketaxess(c):
    """MarketAxess serves its Greenhouse Harvest data through a public proxy: harvest.php?path=/jobs/ (opened_at,
    offices) and /job_posts/ (title, location, HTML content). Detail URL is /careers/current-openings/detail/<slug>-<job_id>.
    Verified live 2026-10-03 (6 open jobs)."""
    base = "https://www.marketaxess.com/harvest.php?path="
    hdr = {"Accept": "application/json"}
    opened = {str(j.get("id")): str(j.get("opened_at") or "")[:10] for j in http(base + "/jobs/", headers=hdr, timeout=40)}
    out = []
    for p in http(base + "/job_posts/", headers=hdr, timeout=40):
        jid = str(p.get("job_id") or p.get("id"))
        title = p.get("title", "") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        out.append(dict(id=jid, title=title, location=(p.get("location") or {}).get("name", ""),
                        url=f"https://www.marketaxess.com/careers/current-openings/detail/{slug}-{jid}",
                        posted=opened.get(jid, ""), desc=strip_html(p.get("content") or "")))
    return out, True


def _tal_fields(page):
    """Label -> value for the read-only form fields on a Tal.net posting page (Region, Location, Group, ...), plus the
    job description text (the field that follows the "Job description" label)."""
    fields = {}
    for m in re.finditer(r'hform_lbl_text"\s*>\s*(.*?)\s*</span>.*?form-control-static"[^>]*>\s*(.*?)\s*</div>', page, re.S):
        fields.setdefault(strip_html(m.group(1)), strip_html(m.group(2)))
    i = page.find("Job description")
    fields["_desc"] = strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page[i:]))[:30000] if i >= 0 else ""
    return fields


def tal(c):
    """Tal.net (Oleeo) candidate job boards, e.g. evercore.tal.net. Each board (vacancy N) publishes a public Atom feed at
    /vx/mobile-0/appcentre-1/candidate/jobboard/vacancy/N/feed (title, posting date, posting URL) and the career site's
    ALTCHA gate only guards applying, not reading. The feed has no place, so each posting page is read once for its
    Location (and Region) field and description, 8 at a time. Needs host and vacancies (board numbers, default [1]).
    Verified live 2026-10-11 against Evercore (vacancies 2 and 3: 96 jobs)."""
    base = f"https://{c['host']}"
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries, ok = {}, True
    for n in c.get("vacancies") or [1]:
        root = ET.fromstring(http(f"{base}/vx/mobile-0/appcentre-1/candidate/jobboard/vacancy/{n}/feed", raw=True, timeout=40,
                                  headers={"User-Agent": BROWSER_UA}))
        for e in root.findall("a:entry", ns):
            url = (e.findtext("a:id", "", ns) or "").strip()
            m = re.search(r"/opp/(\d+)-", url)
            if m:
                entries.setdefault(m.group(1), (url, (e.findtext("a:title", "", ns) or "").strip(), (e.findtext("a:published", "", ns) or "")[:10]))

    def one(item):
        jid, (url, title, posted) = item
        try:
            f = _tal_fields(http(url, raw=True, timeout=40, headers={"Accept": "text/html, */*", "User-Agent": BROWSER_UA}).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001 - one bad posting page must not drop the board; flag the list incomplete instead
            return None
        loc = f.get("Location", "")
        region = f.get("Region", "")
        return dict(id=jid, title=title, url=url, posted=posted, desc=f["_desc"],
                    location=loc if not region or region in loc else f"{loc}, {region}" if loc else region)

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, entries.items()))
    ok = all(r is not None for r in rows)
    return [r for r in rows if r], ok and bool(entries)


def dayforce(c):
    """Dayforce candidate portals (jobs.dayforcehcm.com/<culture>/<namespace>/<board>). The portal's own search call
    POST /api/geo/<namespace>/jobposting/search pages 25 postings at a time (paginationStart = offset) with title,
    full description, exact posting timestamp and structured locations. It wants the NextAuth CSRF token from
    GET /api/auth/csrf (and its cookie) echoed in X-CSRF-Token, an ordinary session handshake. Needs namespace and
    board (jobBoardCode). Job page is <portal>/jobs/<jobPostingId>. Verified live 2026-10-11 against Interactive
    Brokers (182 jobs, about 77 with a US place)."""
    host, ns, board = c.get("host", "jobs.dayforcehcm.com"), c["namespace"], c["board"]
    portal = f"https://{host}/en-US/{ns}/{board}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    hdr = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*", "Referer": portal}
    tok = json.loads(opener.open(urllib.request.Request(f"https://{host}/api/auth/csrf", headers=hdr), timeout=40).read()).get("csrfToken", "")
    out, seen, start = [], set(), 0
    while start < int(c.get("max_jobs", 2000)):
        body = {"clientNamespace": ns, "jobBoardCode": board, "cultureCode": "en-US", "distanceUnit": 0, "paginationStart": start}
        d = json.loads(opener.open(urllib.request.Request(f"https://{host}/api/geo/{ns}/jobposting/search", data=json.dumps(body).encode(),
                                                          headers=dict(hdr, **{"Content-Type": "application/json", "X-CSRF-Token": tok})), timeout=60).read())
        rows = d.get("jobPostings") or []
        for r in rows:
            jid = str(r.get("jobPostingId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            places = [l.get("formattedAddress") for l in r.get("postingLocations") or [] if l.get("formattedAddress")]
            if r.get("hasVirtualLocation"):
                places.append("Remote")
            out.append(dict(id=jid, title=(r.get("jobTitle") or "").strip(), location=" | ".join(places), url=f"{portal}/jobs/{jid}",
                            posted=str(r.get("postingStartTimestampUTC") or "")[:10], desc=strip_html(r.get("jobDescription") or "")))
        start += len(rows)
        if not rows or start >= int(d.get("maxCount") or 0):
            return out, True
    return out, False


def _rippling_text(d):
    desc = d.get("description")
    parts = list(desc.values()) if isinstance(desc, dict) else [desc or ""]
    return strip_html(" ".join(p for p in parts if isinstance(p, str)))


def rippling(c):
    """Rippling ATS boards (ats.rippling.com/<slug>/jobs). The page's own API /api/v2/board/<slug>/jobs lists every
    posting (pageSize 100, page is 0-based, totalPages stated) but carries no date or text, so each posting's
    /jobs/<uuid> detail is read too: exact createdOn plus the description. Needs slug. Verified live 2026-10-12
    against Webull (23 jobs)."""
    items, seen, page, d = [], set(), 0, {}
    base = f"https://ats.rippling.com/api/v2/board/{c['slug']}/jobs"
    while page < int(c.get("max_pages", 50)):
        d = http(f"{base}?page={page}&pageSize=100", headers={"Accept": "application/json"}, timeout=40)
        for j in d.get("items") or []:
            if j.get("id") and j["id"] not in seen:
                seen.add(j["id"])
                items.append(j)
        page += 1
        if page >= int(d.get("totalPages") or 0):
            break
    complete = page >= int(d.get("totalPages") or 0) and len(items) >= int(d.get("totalItems") or 0)

    def one(j):
        try:
            det = http(f"{base}/{j['id']}", headers={"Accept": "application/json"}, timeout=40)
        except Exception:  # noqa: BLE001  a failed detail keeps the row, undated
            det = {}
        locs = [l.get("name") for l in j.get("locations") or [] if l.get("name")] or det.get("workLocations") or []
        if any((l.get("workplaceType") or "") == "REMOTE" for l in j.get("locations") or []):
            locs = locs + ["Remote"]
        return dict(id=j["id"], title=(j.get("name") or "").strip(), location=" | ".join(locs),
                    url=j.get("url") or f"https://ats.rippling.com/{c['slug']}/jobs/{j['id']}",
                    posted=str(det.get("createdOn") or "")[:10], desc=_rippling_text(det))
    return list(_pool_map(one, items, int(c.get("detail_workers", 6)))), complete


def careerpuck(c):
    """CareerPuck job boards (app.careerpuck.com/job-board/<slug>). The page's own call
    GET api.careerpuck.com/v1/public/job-boards/<slug> returns every posting in one response with full (entity
    encoded) content, location and an exact postedAt; one row per posting-location permalink. Needs slug. Verified
    live 2026-10-12 against Fortitude Re (35 rows)."""
    d = http(f"https://api.careerpuck.com/v1/public/job-boards/{c['slug']}", headers={"Accept": "application/json"}, timeout=40)
    out, seen = [], set()
    for j in d.get("jobs") or []:
        jid = str(j.get("permalink") or "")
        if not jid or jid in seen or j.get("status") not in (None, "public"):
            continue
        seen.add(jid)
        places = [j["location"]] if j.get("location") else [o.get("name") for o in j.get("offices") or [] if o.get("name")]  # offices lists every site of the role
        text = strip_html(j.get("content") or "")
        if j.get("salaryDescription"):
            text += " " + strip_html(j["salaryDescription"])
        out.append(dict(id=jid, title=(j.get("title") or "").strip(), location=" | ".join(dict.fromkeys(places)),
                        url=j.get("publicUrl") or f"https://app.careerpuck.com/job-board/{c['slug']}/job/{j.get('atsSourceId') or jid}",
                        posted=str(j.get("postedAt") or "")[:10], desc=text))
    return out, True


def _bamboohr_row(base, j):
    try:
        det = http(f"{base}/careers/{j['id']}/detail", headers={"Accept": "application/json"}, timeout=40)["result"]["jobOpening"]
    except Exception:  # noqa: BLE001  a failed detail keeps the row, undated
        det = {}
    loc = j.get("location") or {}
    place = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
    if j.get("isRemote"):
        place = (place + " | Remote") if place else "Remote"
    return dict(id=str(j["id"]), title=(j.get("jobOpeningName") or "").strip(), location=place,
                url=f"{base}/careers/{j['id']}", posted=str(det.get("datePosted") or "")[:10], desc=strip_html(det.get("description") or ""))


def bamboohr(c):
    """BambooHR career pages (<slug>.bamboohr.com/careers). GET /careers/list returns every opening in one response
    (no paging) but no text or date, so each /careers/<id>/detail is read too: exact datePosted plus the description.
    Needs slug. Verified live 2026-10-17 against Splitit (4 jobs)."""
    base = f"https://{c['slug']}.bamboohr.com"
    d = http(f"{base}/careers/list", headers={"Accept": "application/json"}, timeout=40)
    rows = [j for j in d.get("result") or [] if j.get("id")]
    return list(_pool_map(lambda j: _bamboohr_row(base, j), rows, int(c.get("detail_workers", 4)))), True


def sitecore_search(c):
    """Sitecore Search (Discover) career sites, e.g. careers.kroll.com. The site's browser bundle carries a public
    apiKey and customerKey; the results widget POSTs to discover.sitecorecloud.io/discover/v2/<domain hash> with
    rfk_id rfkid_7 (the stock content_grid widget) and the entity name, and pages with offset/limit (100 per call,
    total_item stated). Rows carry job_url, exact posted_date and the full description. Needs discover_id (the part
    of customerKey after the dash), api_key and entity. Verified live 2026-10-12 against Kroll (366 jobs)."""
    url = f"https://discover.sitecorecloud.io/discover/v2/{c['discover_id']}"
    out, seen, offset, total = [], set(), 0, None
    while total is None or offset < total:
        body = {"context": {"page": {"uri": c.get("page_uri", "/listing-page")}, "locale": {"country": "us", "language": "en"}},
                "widget": {"items": [{"rfk_id": c.get("rfk_id", "rfkid_7"), "entity": c["entity"],
                                      "search": {"content": {}, "limit": 100, "offset": offset}}]}}
        w = (http(url, data=body, headers={"Authorization": c["api_key"]}, timeout=60).get("widgets") or [{}])[0]
        rows = w.get("content") or []
        total = int(w.get("total_item") or 0)
        for j in rows:
            jid = str(j.get("job_id") or j.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            city = j.get("job_primary_city") or ", ".join(j.get("job_city") or [])
            country = j.get("job_primary_country") or ", ".join(j.get("job_country") or [])
            loc = ", ".join(x for x in (city, j.get("job_primary_region") or "", country) if x)
            if j.get("working_mode") == "Remote":
                loc += " | Remote"
            out.append(dict(id=jid, title=(j.get("name") or "").strip(), location=loc, url=j.get("job_url") or "",
                            posted=str(j.get("posted_date") or "")[:10], desc=strip_html(j.get("job_description") or "")))
        offset += len(rows)
        if not rows:
            break
    return out, bool(total) and len(seen) >= total


_BALYASNY_SITE = "https://bambusdev.my.site.com"  # BAM's careers app lives on its Salesforce Experience Cloud site


def _balyasny_aura(classname, method, params):
    """One anonymous Salesforce Aura call to an @AuraEnabled Apex method the public careers page itself makes.
    The framework id (fwuid) and app build id are read from the /s/ page each time, so a Salesforce release
    does not break the call."""
    page = urllib.parse.unquote(http(_BALYASNY_SITE + "/s/", raw=True, timeout=40, headers={"User-Agent": BROWSER_UA}).decode("utf-8", "ignore"))
    m = re.search(r'"fwuid":"([^"]+)","loaded":\{"APPLICATION@markup://siteforce:communityApp":"([^"]+)"', page)
    if not m:
        raise ValueError("Balyasny Aura context not found")
    ctx = {"mode": "PROD", "fwuid": m.group(1), "app": "siteforce:communityApp", "dn": [], "globals": {}, "uad": True,
           "loaded": {"APPLICATION@markup://siteforce:communityApp": m.group(2)}}
    msg = {"actions": [{"id": "1;a", "descriptor": "aura://ApexActionController/ACTION$execute", "callingDescriptor": "UNKNOWN",
                        "params": {"namespace": "", "classname": classname, "method": method, "params": params,
                                   "cacheable": True, "isContinuation": False}}]}
    body = urllib.parse.urlencode({"message": json.dumps(msg), "aura.context": json.dumps(ctx), "aura.pageURI": "/s/", "aura.token": "null"}).encode()
    raw = http(_BALYASNY_SITE + "/s/sfsites/aura?r=2&aura.ApexAction.execute=1", data=body, raw=True, timeout=60,
               headers={"User-Agent": BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    act = (json.loads(raw).get("actions") or [{}])[0]
    if act.get("state") != "SUCCESS":
        raise ValueError(f"Balyasny Aura call failed: {act.get('error')}")
    return (act.get("returnValue") or {}).get("returnValue")


def balyasny(c):
    """Balyasny Asset Management's careers site is a Salesforce Experience Cloud (Aura) app with no REST feed, but
    its public Apex method BamJobRequisitionInfoDataService.searchJobRequisitions answers anonymous POSTs to
    /s/sfsites/aura with every open requisition (title, REQ number, per-position cities, relative "Posted N Days
    Ago"), so one call lists the lot. Job id is the Salesforce record id; describe() fetches the description with
    rolePage.getDescription(id). Detail page is /s/details?jobReq=<url title>_<REQ>. Verified live 2026-10-11
    (129 jobs, about 60 with a US city)."""
    rows = _balyasny_aura("BamJobRequisitionInfoDataService", "searchJobRequisitions",
                          {"isVendorPortal": False, "site": "BAM Website", "searchKey": "", "locationFilters": [],
                           "departmentFilter": [], "availableLocations": [], "experienceLevelFilter": []}) or []
    out = []
    for r in rows:
        cities = []
        for pos in r.get("Job_Requisition_Positions__r") or []:  # a role can list several offices, including secondary ones
            for loc in [pos.get("Location__r") or {}] + [(q.get("Location__r") or {}) for q in pos.get("Job_Requisition_Position_Locations__r") or []]:
                name = loc.get("External_Name__c")
                if name and name not in cities:
                    cities.append(name)
        req = r.get("Requisition_Number__c") or ""
        out.append(dict(id=r["Id"], title=r.get("Publish_Title__c") or r.get("Name") or "", location=" | ".join(cities),
                        url=f"{_BALYASNY_SITE}/s/details?jobReq={r.get('Job_Req_Title_in_URL__c') or ''}_{req}",
                        posted=_workday_posted(r.get("Posted_Ago__c"))))  # relative text, so the date is approximate
    return [j for j in out if j["title"]], bool(rows)


def phenom(c):
    """Phenom People career sites. POST https://<host>/widgets with ddoKey "refineSearch" after one GET of the
    search page for the session cookie. Needs host, ref_num (the site's refNum), page_id (the search page's
    pageId) and optionally path (search page path, default /us/en/search-results) and lang/country. The list
    carries title, location and an exact postedDate; descriptions come from each job page's JSON-LD.
    Verified live 2026-10-03 against Franklin Templeton (198 jobs)."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdr = {"User-Agent": UA["User-Agent"]}
    host, country = c["host"], c.get("country", "us")
    opener.open(urllib.request.Request(f"https://{host}{c.get('path', '/us/en/search-results')}", headers=hdr), timeout=40).read()
    out, seen, offset, size = [], set(), 0, 50
    while offset < int(c.get("max_jobs", 2000)):
        body = {"sortBy": "", "subsearch": "", "from": offset, "jobs": True, "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"], "pageName": "search-results", "size": size,
                "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "pageId": c["page_id"], "siteType": "external",
                "keywords": "", "global": True, "selected_fields": {}, "sort": {"order": "desc", "field": "postedDate"},
                "lang": c.get("lang", "en_us"), "deviceType": "desktop", "country": country, "refNum": c["ref_num"], "ddoKey": "refineSearch"}
        raw = opener.open(urllib.request.Request(f"https://{host}/widgets", data=json.dumps(body).encode(),
                                                 headers=dict(hdr, **{"Content-Type": "application/json"})), timeout=40).read()
        data = json.loads(raw).get("refineSearch") or {}
        rows = (data.get("data") or {}).get("jobs") or []
        for r in rows:
            jid = str(r.get("jobSeqNo") or r.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            slug = re.sub(r"[^A-Za-z0-9]+", "-", r.get("title", "") or "").strip("-")
            out.append(dict(id=jid, title=r.get("title", "") or "", location=r.get("location") or r.get("cityStateCountry") or "",
                            url=f"https://{host}/{c.get('url_country', country)}/{c.get('lang', 'en_us')[:2]}/job/{jid}/{slug}",
                            posted=str(r.get("postedDate") or r.get("dateCreated") or "")[:10]))
        if len(rows) < size:
            return out, True
        offset += size
    return out, False


FETCH = dict(greenhouse=greenhouse, lever=lever, ashby=ashby, smartrecruiters=smartrecruiters, workday=workday,
             oracle=oracle, personio=personio, goldman=goldman, successfactors=successfactors, icims=icims, eightfold=eightfold, talentbrew=talentbrew, avature=avature, kpmg=kpmg, adp_rm=adp_rm, adp_wfn=adp_wfn, ukg=ukg, taleo_be=taleo_be, taleo_cws=taleo_cws, csod=csod, peoplesoft_hrs=peoplesoft_hrs, talentsoft=talentsoft, bpce=bpce, paylocity=paylocity, deshaw=deshaw, brassring=brassring, jibe=jibe, taleo=taleo, hrmdirect=hrmdirect, commerzbank=commerzbank, dzbank=dzbank, marketaxess=marketaxess, phenom=phenom,
             balyasny=balyasny, tal=tal,
             dayforce=dayforce, rippling=rippling, careerpuck=careerpuck, sitecore_search=sitecore_search, bamboohr=bamboohr)
STALE_ALERT_HOURS = 10  # page shows a red banner when the last scout run is older than this
FETCH_WORKERS = 16  # fetches are I/O-bound (network wait); parallelizing across companies cuts wall-clock a lot


def fetch_company(c):
    """Runs on a worker thread. Retries once after a 5s sleep on any error - absorbs a scheduled
    run firing right as the runner's network isn't fully up yet."""
    try:
        jobs, complete = FETCH[c["ats"]](c)
        return c["name"], jobs, complete, None
    except Exception:
        time.sleep(5)
        try:
            jobs, complete = FETCH[c["ats"]](c)
            return c["name"], jobs, complete, None
        except Exception as e2:
            return c["name"], None, None, e2


def _timed_fetch(c):
    start = time.time()
    name, jobs, complete, err = fetch_company(c)
    return name, jobs, complete, err, time.time() - start


SHARD_BUDGET_SECONDS = 720  # a shard job is killed at 15 min, which cancelled the whole run; stragglers become errors (skip, close nothing) instead


def fetch_all(companies, budget=SHARD_BUDGET_SECONDS):
    fetched, secs = {}, {}
    _FETCH_DEADLINE[0] = time.time() + budget
    pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
    futures = {pool.submit(_timed_fetch, c): c for c in companies}
    done, pending = wait(futures, timeout=budget)
    for fut in done:
        name, jobs, complete, err, took = fut.result()
        fetched[name] = (jobs, complete, err)
        secs[name] = took
    for fut in pending:
        name = futures[fut]["name"]
        fetched[name] = (None, None, f"fetch exceeded the {budget}s shard budget")
        secs[name] = float(budget)
    pool.shutdown(wait=False, cancel_futures=True)
    return fetched, secs


def _fetch_cost(c):
    if c["ats"] != "workday":
        return 1
    sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
    terms = _terms(c)
    return len(sites) * (1 if isinstance(terms, str) else len(terms))


FETCH_TIMES_PATH = HOME / "fetch_times.json"  # committed, so every shard job of a run splits identically


def shard_companies(companies, index, total, times=None):
    """Deterministic longest-first split. A shard's wall time is about max(longest single company, summed
    seconds / FETCH_WORKERS), not the plain sum, so each company goes where that estimate grows least.
    Uses measured seconds per company (call-count estimate for one with no measurement yet)."""
    times = times or {}
    ordered = sorted(companies, key=lambda c: (-times.get(c["name"], _fetch_cost(c)), c["name"]))
    sums, longest = [0.0] * total, [0.0] * total
    mine = []
    for c in ordered:
        t = times.get(c["name"], _fetch_cost(c))
        k = min(range(total), key=lambda i: (max((sums[i] + t) / FETCH_WORKERS, longest[i], t), sums[i], i))
        sums[k] += t
        longest[k] = max(longest[k], t)
        if k == index:
            mine.append(c)
    return mine


def update_fetch_times(directory, path=None):
    """Blends each shard file's measured seconds into the stored per-company times (moving average, so one 429-retry spike doesn't skew the split)."""
    path = Path(path or FETCH_TIMES_PATH)
    times = load_json(path, {})
    for p in sorted(Path(directory).glob("*.json")):
        for name, row in json.loads(p.read_text(encoding="utf-8")).items():
            took = row.get("secs")
            if took is not None:
                times[name] = round(0.5 * times[name] + 0.5 * took, 2) if name in times else round(took, 2)
    atomic_json.write(str(path), times)


def cmd_fetch_shard(args):
    cfg = load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}.")
    _load_title_terms(cfg)
    index, total = (int(x) for x in args.fetch_shard.split("/"))
    mine = shard_companies(cfg["companies"], index, total, load_json(FETCH_TIMES_PATH, {}))
    fetched, secs = fetch_all(mine)
    out = {name: dict(jobs=jobs, complete=complete, err=None if err is None else str(err), secs=secs[name])
           for name, (jobs, complete, err) in fetched.items()}
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    print(f"shard {index}/{total}: fetched {len(out)} companies -> {args.out}")
    sys.stdout.flush()
    os._exit(0)  # budget stragglers are still running in non-daemon threads


BOARD_FRESHNESS_PATH = HOME / "board_freshness.json"  # per company: posting count, id signature and the date that signature last changed


def update_board_freshness(fetched, path=None):
    """Records when each board's set of posting ids last changed, so a frozen or emptied board can be reported by board_freshness.py."""
    path = Path(path or BOARD_FRESHNESS_PATH)
    rec = load_json(path, {})
    today = date.today().isoformat()
    for name, (jobs, complete, err) in fetched.items():
        if err is not None or jobs is None:
            continue
        sig = hashlib.md5(",".join(sorted(str(j["id"]) for j in jobs)).encode()).hexdigest()[:12]
        old = rec.get(name)
        rec[name] = dict(n=len(jobs), sig=sig, checked=today,
                         changed=today if not old or old.get("sig") != sig else old.get("changed", today))
    atomic_json.write(str(path), rec)


def load_fetched(directory, companies):
    """Merges every shard file; a company missing from all of them (failed shard) is an error, which
    run() already treats as skip-without-closing-anything."""
    merged = {}
    for p in sorted(Path(directory).glob("*.json")):
        merged.update(json.loads(p.read_text(encoding="utf-8")))
    return {c["name"]: ((merged[c["name"]]["jobs"], merged[c["name"]]["complete"],
                         merged[c["name"]]["err"]) if c["name"] in merged
                        else (None, None, "shard result missing"))
            for c in companies}


def _page_date(raw):
    """Posting date (YYYY-MM-DD) from a job page's datePosted field, ISO or "Oct 1, 2026"; "" if absent."""
    m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', raw)
    if not m:
        return ""
    v = m.group(1).strip()
    if re.match(r"\d{4}-\d\d-\d\d", v):
        return v[:10]
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _brassring_detail(c, jid):
    """Description for one BrassRing req: same cookie + RFT session as brassring(), then the page's own
    Ajax/JobDetails call, whose textarea answers (responsibilities, skills, about us...) are the posting body."""
    import http.cookiejar
    base = f"https://{c['host']}"
    pid, sid = str(c["partnerid"]), str(c["siteid"])
    ref = f"{base}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={pid}&siteid={sid}&PageType=JobDetails&jobid={jid}"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    page = opener.open(urllib.request.Request(ref, headers={"User-Agent": BROWSER_UA}), timeout=60).read().decode("utf-8", "ignore")
    tok = next(ck.value for ck in cj if ck.name == f"tg_session_{pid}_{sid}")
    rft = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page).group(1)
    body = {"partnerId": pid, "siteId": sid, "jobid": str(jid), "configMode": "", "jobSiteId": sid, "encryptedSessionValue": tok}
    req = urllib.request.Request(base + "/TgNewUI/Search/Ajax/JobDetails", data=json.dumps(body).encode(), headers={
        "User-Agent": BROWSER_UA, "Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01", "Referer": ref, "Origin": base, "RFT": rft})
    d = json.loads(opener.open(req, timeout=60).read())
    qs = ((d.get("ServiceResponse") or {}).get("Jobdetails") or {}).get("JobDetailQuestions") or []
    return strip_html(" ".join(f"{q.get('QuestionName') or ''} {q.get('AnswerValue') or ''}" for q in qs
                               if q.get("QuestionType") == "textarea"))[:30000]


def _ukg_detail(c, job):
    """Full description for a UKG board posting: the detail page embeds the opportunity as JSON in a
    CandidateOpportunityDetail({...}) call, with the HTML text in its Description field."""
    req = urllib.request.Request(job["url"], headers={"User-Agent": BROWSER_UA})
    page = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "ignore")
    m = re.search(r"CandidateOpportunityDetail\(", page)
    if not m:
        return ""
    obj, _ = json.JSONDecoder().raw_decode(page, m.end())
    text = strip_html(obj.get("Description") or "")
    pay = obj.get("PayRange") or {}  # the posted range lives in its own field, not in the description text
    if obj.get("PayRangeVisible") and pay.get("PayRangeMinimum") and pay.get("PayRangeMaximum"):
        text += f" Pay range: ${float(pay['PayRangeMinimum']):,.0f} to ${float(pay['PayRangeMaximum']):,.0f}"
    return text


MULTISITE_CACHE = HOME / "multisite_cache.json"  # job id -> full site list, so a rejected multi-site posting is fetched once
_MULTISITE_LOCK = threading.Lock()
MULTISITE_MAX_FETCH = 150  # new detail fetches per run (Workday 429s on bursts)


def _rescue_multi_site(c, jobs, hits, filters):
    """Workday lists "N Locations (first city)", so the location filters judge only the first site. For multi-site
    postings the title filters pass but the location filters reject, read the full site list from the detail
    response and re-run matches() per site (one acceptable site is enough). Returns the rescued jobs (location replaced)."""
    if c.get("ats") != "workday":
        return []
    in_hits = {j["id"] for j in hits}
    title_only = dict(filters, locations_include=[], locations_exclude=[])
    cand = [j for j in jobs if j["id"] not in in_hits and _MULTI_LOC_RE.match((j.get("location") or "").split(" (")[0])
            and matches(dict(j, location="United States"), title_only)]
    if not cand:
        return []
    cache = load_json(MULTISITE_CACHE, {})
    fetched, rescued, new_keys = 0, [], {}
    for j in cand:
        key = f"{c['name']}:{j['id']}"
        full = cache.get(key)
        if full is None:
            if fetched >= MULTISITE_MAX_FETCH:
                continue
            fetched += 1
            probe = dict(j)
            try:
                describe(c, probe)
            except Exception:  # noqa: BLE001  detail unreachable: leave it for a later run
                continue
            finally:
                time.sleep(0.3)
            full = cache[key] = new_keys[key] = probe["location"]
        if full != j["location"] and any(matches(dict(j, location=site), filters) for site in full.split("; ")):
            rescued.append(dict(j, location=full))
    if new_keys:
        with _MULTISITE_LOCK:  # companies are rescued in parallel: merge into the latest file, never overwrite it
            latest = load_json(MULTISITE_CACHE, {})
            latest.update(new_keys)
            atomic_json.write(str(MULTISITE_CACHE), latest)
    return rescued


def describe(c, job):
    """Full description text for one job, plus a Workday date refinement. Returns (text, posted,
    posted_source) - posted_source is "exact" for Workday's detail-endpoint startDate and the other
    detail-page dates (iCIMS/Avature/KPMG JSON-LD, ADP RM search-meta, Two Sigma feed), "approx" for
    Workday's relative-text fallback and update-date proxies (Talentsoft, iCIMS sitemap lastmod), and ""
    otherwise (the list call already returns an exact native date, or the site publishes none - see the
    per-fetcher functions above). Lever/
    Ashby already carry the description from the list call."""
    ats = c["ats"]
    if ats == "adp_rm":  # the list call carries the text, but only this endpoint has the posting date
        text, posted = _adp_rm_detail(c, job["id"])
        return job.get("desc") or strip_html(text), posted, "exact" if posted else ""
    if job.get("desc"):
        return job["desc"], "", ""
    if ats == "ukg":
        return _ukg_detail(c, job), "", ""
    if ats == "greenhouse":
        d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs/{job['id']}")
        pay = ""
        for r in d.get("pay_input_ranges") or []:  # the posted range is a structured field, often absent from the text
            lo, hi = (r.get("min_cents") or 0) / 100, (r.get("max_cents") or 0) / 100
            unit = "hour" if 0 < hi <= 400 else "year" if hi >= 20000 else ""  # Greenhouse states no unit; skip the ambiguous middle
            pay += _pay_text(r.get("currency_type"), lo, hi, unit)
        return strip_html(d.get("content", "")) + pay, "", ""
    if ats == "smartrecruiters":
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{job['id']}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return strip_html(" ".join((s or {}).get("text", "") for s in secs.values())), "", ""
    if ats == "workday":
        cand = [job["site"]] if job.get("site") else (c["site"] if isinstance(c["site"], list) else [c["site"]])
        d, err = None, None
        for site in cand:  # queue entries without a per-job site try each configured site
            try:
                d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}{job['id']}")
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if d is None:
            raise err
        info = d.get("jobPostingInfo") or {}
        posted, posted_source = _workday_detail_posted(info)
        full = _workday_full_location(info)
        if full and _MULTI_LOC_RE.match((job.get("location") or "").split(" (")[0]):
            job["location"] = full  # replaces "2 Locations (City)" with every site the posting lists
        return strip_html(info.get("jobDescription", "")), posted, posted_source
    if ats == "oracle":
        finder = f"ById;Id={job['id']}"
        url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
               + urllib.parse.urlencode({"onlyData": "true", "finder": finder}))
        d = http(url)
        items = d.get("items") or []
        if not items:
            return "", "", ""
        it = items[0]
        parts = [it.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
        return strip_html(" ".join(p for p in parts if p)), "", ""
    if ats == "jibe":  # list rows are queued without desc; the job page JSON-LD carries it
        return (_jsonld_description(job["url"]) if job.get("url") else ""), "", ""
    if ats == "tal":  # list rows keep no description; the posting page carries it (same parser tal() uses)
        if not job.get("url"):
            return "", "", ""
        page = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*", "User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
        return _tal_fields(page).get("_desc", "")[:30000], "", ""
    if ats == "dayforce":  # the search call returns each posting's description; pick ours out of the board list
        rows, _ = dayforce(c)
        hit = next((r for r in rows if str(r["id"]) == str(job["id"])), None)
        return ((hit or {}).get("desc", "") or "")[:30000], ((hit or {}).get("posted", "") or ""), ("exact" if hit and hit.get("posted") else "")
    if ats == "peoplesoft_hrs":  # the detail page opens in a fresh cookie session; the posting text starts at "JOB OPENING"
        if not job.get("url"):
            return "", "", ""
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        page = opener.open(urllib.request.Request(job["url"], headers={"User-Agent": BROWSER_UA}), timeout=40).read().decode("utf-8", "ignore")
        text = strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page))
        i = text.find("JOB OPENING")
        return (text[i:] if i >= 0 else "")[:30000], "", ""
    if ats == "taleo_cws":  # list rows have no description or date; the requisition page JSON-LD carries both
        url = job.get("url") or ""
        if not url:
            return "", "", ""
        raw = http(url, raw=True, timeout=40, headers={"User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
        for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("description"):
                dp = str(data.get("datePosted") or "")[:10]
                return strip_html(data["description"])[:30000], dp, ("exact" if dp else "")
        return "", "", ""
    if ats == "icims":
        return _icims_description(job)
    if ats == "adp_wfn":
        d = http(f"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions/{job['id']}"
                 f"?cid={c['cid']}&ccId={c.get('ccId', '19000101_000001')}&lang=en_US&locale=en_US",
                 headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"})
        return strip_html(d.get("requisitionDescription", "")), "", ""
    if ats in ("avature", "kpmg"):
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        posted = _page_date(raw)  # the JobPosting JSON-LD carries the real posting day the list cards lack
        if not posted and c.get("feed") and not job.get("posted"):
            posted = _avature_feed_date(c, job)  # Two Sigma pages have no JSON-LD; its feed does
        pay = _jsonld_pay(raw)
        raw = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw)
        return strip_html(raw)[:30000] + pay, posted, "exact" if posted else ""  # Avature job pages carry no JSON-LD; whole-page text is enough for the sponsorship/experience regexes
    if ats == "balyasny":  # the list call carries no description
        d = _balyasny_aura("rolePage", "getDescription", {"req": job["id"]}) or {}
        return strip_html(d.get("Description__c", "")), "", ""
    if ats == "taleo_be":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*", "User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
        i = raw.find('class="job_description"')
        return (strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw[i:]))[:30000] if i >= 0 else ""), "", ""
    if ats == "talentsoft":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*", "User-Agent": BROWSER_UA}).decode("utf-8", "ignore")
        i = raw.find("ts-offer-page__body")
        upd = re.search(r'ts-offer-page__maj-date">\s*<h3>[^<]*</h3>\s*(\d\d)/(\d\d)/(\d{4})', raw)  # "Date de mise a jour", dd/mm/yyyy
        posted = f"{upd.group(3)}-{upd.group(2)}-{upd.group(1)}" if upd else ""
        return (strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw[i:]))[:30000] if i >= 0 else ""), posted, "approx" if posted else ""
    if ats == "commerzbank":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        return strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw))[:30000], "", ""
    if ats == "phenom":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        for m in re.finditer(r'"description":"((?:[^"\\]|\\.)*)"', raw):
            try:
                text = strip_html(json.loads('"' + m.group(1) + '"'))
            except ValueError:
                continue
            if len(text) > 200:  # the page also carries short site-level descriptions
                return text + _jsonld_pay(raw), "", ""
        return _jsonld_description(job["url"]), "", ""
    if ats in ("talentbrew", "paylocity"):
        return _jsonld_description(job["url"]), "", ""
    if ats == "taleo":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        i = raw.find("api.fillList('requisitionDescriptionInterface', 'descRequisition'")
        if i < 0:
            return "", "", ""
        arr = raw[i:raw.find("]);", i)]
        parts = [urllib.parse.unquote(m) for m in re.findall(r"'([^']*)'", arr)[3:] if len(m) > 40]  # description fields are the long URL-encoded strings
        return strip_html(" ".join(p.replace("!*!", " ") for p in parts))[:30000], "", ""
    if ats == "successfactors":  # list call carries desc, but a queue entry has only its link; read the page's description block
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        i = raw.find('itemprop="description"')
        if i < 0:
            return "", "", ""
        return strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw[raw.find(">", i) + 1:]))[:30000], "", ""
    if ats == "lever":  # queue rows carry no desc; the single-posting API returns the fields lever() builds it from
        host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
        j = http(f"https://{host}/v0/postings/{c['slug']}/{job['id']}")
        text = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists") or []:
            text += " " + (lst.get("text") or "") + " " + strip_html(lst.get("content", ""))
        return " ".join((text + " " + (j.get("additionalPlain") or "")).split())[:30000], "", ""
    if ats == "deshaw":  # JSON-LD holds only the intro paragraph; the full text is in __NEXT_DATA__ jobDescription
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
        if not m:
            return "", "", ""
        desc = (((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}).get("jobData") or {}).get("jobDescription") or {}
        return " ".join(strip_html(desc.get(k) or "") for k in ("websiteDescription", "responsibilitiesHtml", "peopleWeAreLookingForStr")).strip()[:30000], "", ""
    if ats == "hrmdirect":  # static detail page; body text minus scripts/styles, as hrmdirect() reads it
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        return strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw))[:30000], "", ""
    if ats == "goldman":  # the list call returns descriptionHtml but queue rows drop it; role() takes the numeric id from the URL
        src = job["url"].rstrip("/").rsplit("/", 1)[-1] if job.get("url") else str(job["id"]).split("_")[0]
        d = http("https://api-higher.gs.com/gateway/api/v1/graphql", data={
            "query": "query GetRole($id: String!) { role(externalSourceId: $id) { descriptionHtml } }", "variables": {"id": src}})
        return strip_html(((d.get("data") or {}).get("role") or {}).get("descriptionHtml") or "")[:30000], "", ""
    if ats == "brassring":  # list rows carry desc but queue rows do not; the page's own JobDetails Ajax call has it
        return _brassring_detail(c, job["id"]), "", ""
    if ats == "bpce":  # job_number (our id) is not the WP post id: resolve it via the slug, then read the page JSON
        slug = job["url"].rstrip("/").rsplit("/", 1)[-1]
        base = f"https://{c['host']}/app/wp-json"
        hdr = {"User-Agent": BROWSER_UA}
        hit = http(f"{base}/wp/v2/job?slug={urllib.parse.quote(slug)}&lang=en", headers=hdr)
        if not hit:
            return "", "", ""
        d = http(f"{base}/bpce/v1/posts/?lang=en&_uid=job-{hit[0]['id']}-x", headers=hdr, timeout=60)
        return strip_html(((d.get("content") or {}).get("main") or {}).get("text") or "")[:30000], "", ""
    if ats == "eightfold":
        return strip_html(_eightfold_detail(c, job["id"])), "", ""
    if ats == "rippling":
        d = http(f"https://ats.rippling.com/api/v2/board/{c['slug']}/jobs/{job['id']}")
        return _rippling_text(d)[:30000], str(d.get("createdOn") or "")[:10], "exact" if d.get("createdOn") else ""
    if ats == "bamboohr":
        det = http(f"https://{c['slug']}.bamboohr.com/careers/{job['id']}/detail", headers={"Accept": "application/json"})["result"]["jobOpening"]
        return strip_html(det.get("description") or "")[:30000], str(det.get("datePosted") or "")[:10], "exact" if det.get("datePosted") else ""
    if ats in ("careerpuck", "sitecore_search"):  # one call lists every posting with its text; pick ours out
        rows, _ = FETCH[ats](c)
        hit = next((r for r in rows if str(r["id"]) == str(job["id"])), None)
        return ((hit or {}).get("desc", "") or "")[:30000], "", ""
    return "", "", ""


# ------------------------------------------------------------------ analysis

def _fold_accents(s):
    """"México"/"Bogotá"/"São Paulo" -> "Mexico"/"Bogota"/"Sao Paulo" so plain-ASCII
    locations_exclude terms still match accented location strings from non-US ATS entries."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_MEXICO_RE = re.compile(r"(?<!new )(?<![a-z0-9])mexico(?![a-z0-9])")

# "Dublin" collides with two real US instances (Cardinal Health's "OH-Dublin-Cardinal Place" and
# Fifth Third Bank's "Dublin, OH") among otherwise-all-Ireland hits, so it needs the same
# negative-lookaround treatment as Mexico rather than a plain locations_exclude entry.
_DUBLIN_RE = re.compile(r"(?<!oh-)(?<![a-z0-9])dublin(?![a-z0-9])(?!, oh\b)")

# Same story for "Athens": bare "Athens" (PwC's Greece office listings) vs. Gopuff's "Athens, OH"/
# "Athens, GA" (real US college towns) - the state suffix is what disambiguates, not the word itself.
_ATHENS_RE = re.compile(r"(?<![a-z0-9])athens(?![a-z0-9])(?!, oh\b)(?!, ga\b)")

# Bare "Vancouver" (Workday shows just the city) is Canadian; Vancouver, WA is a real US city.
_VANCOUVER_RE = re.compile(r"(?<![a-z0-9])vancouver(?![a-z0-9])(?!, wa\b)(?!, washington\b)")

# FIS (and similar Workday tenants) write locations as "<ISO country code> <site code> ...", e.g.
# "IND HYDB 18-23 OB3"; US sites start with "US", so only non-US country codes are listed here.
# Saxo Bank's Copenhagen roles carry the bare Workday location "Headquarters"; as a locations_exclude term it also
# dropped US sites like "US-NJ-Princeton-100-Headquarters" and "Office - USA - CA - Headquarters", so only the
# whole-string form is excluded.
_BARE_HQ_RE = re.compile(r"^\s*headquarters\s*$")

# Whole-string city names that are foreign on Workday ("OTTAWA", "NORTH YORK") but also real US place names
# ("Ottawa, IL", "Nassau County, NY", "San Sebastian, PR"), so only the bare form is excluded.
_BARE_NON_US_CITY_RE = re.compile(r"^\s*(?:ottawa|north york|nassau|surrey|san sebastian|san jos\ufffd)\s*$")

# Canadian Ontario, but not Ontario, California / Oregon / New York / Ohio.
_ONTARIO_RE = re.compile(r"\bontario\b(?!,?\s*(?:ca|california|or|oregon|ny|oh|ohio)\b)")

# Lever-style boards (Tala) give a bare country code as the whole location ("PH", "MX"). Only codes that are not US
# state or territory abbreviations are listed, so "IN", "DE", "CA", "ID", "AR" never match.
_BARE_NON_US_CODE_RE = re.compile(
    r"^\s*(?:ph|mx|uk|gb|sg|hk|jp|kr|cn|br|pl|au|my|th|vn|tw|nz|ng|ke|pk|bd|lk|ae|il|ch|se|dk|fi|pt|ro|bg|cz|hu|ua|tr|cl)\s*$"
)

# "Wellington, nz" / "Adelaide, AU": trailing non-US country code. Codes that double as US state codes are absent.
_TRAILING_NON_US_CC_RE = re.compile(
    r",\s*(?:nz|au|uk|gb|sg|hk|jp|kr|cn|br|pl|ph|mx|ae|ie|nl|be|fr|es|it|se|dk|fi|pt|ro|bg|cz|hu|ua|tr|cl|za|eg|ch|th|vn|tw|ng|ke|pk|bd|lk|my)\s*$"
)

# "Wellington, nz" / "Adelaide, AU": trailing non-US country code. Codes that are also US state codes
# (de, il, ma, co, ca, in, id, ar, pa, ga, md, me, mt, tn, va, al...) are deliberately absent.
_TRAILING_NON_US_CC_RE = re.compile(
    r",\s*(?:nz|au|uk|gb|sg|hk|jp|kr|cn|br|pl|ph|mx|ae|ie|nl|be|fr|es|it|se|dk|fi|pt|ro|bg|cz|hu|ua|tr|cl|za|eg|ch|th|vn|tw|ng|ke|pk|bd|lk|my)\s*$"
)

_COUNTRY_CODE_SITE_RE = re.compile(
    r"^(ind|esp|pol|phl|cyp|hgk|can|gbr|irl|deu|fra|ita|nld|sgp|jpn|aus|bra|mex|chn|zaf|are|isr|che|swe|nor|dnk"
    r"|fin|prt|rou|bgr|cze|hun|ltu|lva|est|ukr|tur|arg|chl|col|per|pak|bgd|lka|mys|tha|vnm|idn|kor|twn|nzl|egy"
    r"|mar|ken|nga|sau|qat|kwt) [a-z]{4}(?![a-z])"
)

# Generic non-US shapes so each new overseas Workday site stops needing its own exclude term
# (candidates are US-only). "<ISO3> - City" / "IND-BLR-Site", "CA-<province>-" and "DE/FR/...-<state>-"
# triples, Canadian province codes and Canadian postal codes. Raw (un-lowered) location for postal.
_ISO3_DASH_RE = re.compile(
    r"^(ind|esp|pol|phl|cyp|hgk|can|gbr|irl|deu|fra|ita|nld|sgp|jpn|aus|bra|mex|chn|zaf|isr|che|swe|dnk"
    r"|fin|prt|rou|bgr|cze|hun|ltu|lva|ukr|tur|arg|chl|pak|bgd|lka|mys|tha|vnm|idn|kor|twn|nzl|egy|ken|nga"
    r"|sau|qat|kwt)\s*[-|]\s*[a-z]"
)
_ISO2_SUBDIV_RE = re.compile(
    r"^(?:ca-(?:on|bc|qc|ab|ns|nb|mb|sk|nl|pe|yt|nt|nu)-[a-z]|(?:de|fr|it|nl|es|au|jp|cn|br|mx|ie|sg|se|pl)-[a-z]{2,3}-[a-z])"
)
# EY writes "City, region, CC, postcode" ("Adelaide, SA, AU, 5000"; US rows say "US") and sometimes "City, CC, postcode"
# with a non-ZIP postcode ("Diegem, BE, 1831"); a 5-digit ZIP or "US" keeps the US rows.
_EY_NON_US_RE = re.compile(
    r"^[^,]+,\s*[a-z]{1,3},(?!\s*us\b)\s*[a-z]{2},\s*[a-z0-9][a-z0-9 -]*$"
    r"|^[^,]+,\s*[a-z]{2},(?!\s*\d{5}(?:-\d{4})?$)\s*[a-z0-9 -]*\d[a-z0-9 -]*$"
)
_CA_POSTAL_RE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b")
_CA_PROVINCE_CODE_RE = re.compile(r",\s*(?:on|bc|qc|ab|ns|nb|mb|sk|nl|pe)\s*(?:,|$)", re.I)


_WORKDAY_SLUG_RE = re.compile(r"/job/([^/]+)/", re.I)
_TITLE_TAIL_RE = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")


def effective_location(job):
    """Blank-location rows (Fidelity International, Moderna, EY Germany) hide the site in the Workday link slug
    ("Kronberg-Office", "Warsaw---Poland") or in the title's last parenthetical, so the location filters read those."""
    loc = (job.get("location") or "").strip()
    if loc:
        return job["location"]
    m = _WORKDAY_SLUG_RE.search(job.get("link") or "")
    if m and "myworkdayjobs" in (job.get("link") or ""):
        return re.sub(r"-+", " ", m.group(1)).strip()
    m = _TITLE_TAIL_RE.search(job.get("title") or "")
    return m.group(1) if m else ""


# Spring 2027 internships are wanted even though the generic intern/summer/co-op excludes drop other internships.
_SPRING_2027_RE = re.compile(r"\bspring\s*(?:of\s*)?2027\b", re.I)
_INTERN_EXCLUDE_TERMS = {"intern", "internship", "summer", "co-op"}


def matches(job, f):
    # Title include/exclude use word-boundary matching (keyword_matching.contains_term) so a short
    # term like "intern" doesn't false-positive inside "Internal"/"International". locations_include
    # stays plain substring: filters like ", nj" rely on punctuation that word-boundary matching
    # would reject (the comma has no alnum neighbor to anchor against). locations_exclude uses
    # word-boundary matching instead, on an accent-folded location string, so a bare country name
    # matches regardless of where it sits ("Mexico" / "Argentina - Buenos Aires" / "Ciudad de
    # Mexico, Mexico") while still not false-matching inside a longer word ("Indianapolis" for
    # "india"). "Mexico" gets its own regex with a negative lookbehind for "new " since "New
    # Mexico" is a real US state name containing "Mexico" as its own bounded word - no amount of
    # word-boundary anchoring alone can tell those apart. "Georgia" (also a real US state name) and
    # "Jersey" (a Channel Island, also a real US state's shorthand) are deliberately left out of
    # locations_exclude for the same reason and aren't worth the collision risk.
    job_loc = effective_location(job)
    t, loc = " ".join(job["title"].split()), job_loc.lower()  # collapse double spaces so excludes match
    loc_folded = _fold_accents(loc)
    inc = f.get("title_include", [])
    exc = f.get("title_exclude", [])
    locs = [x.lower() for x in f.get("locations_include", [])]
    loc_exc = f.get("locations_exclude", [])
    spring_2027 = bool(_SPRING_2027_RE.search(t))
    if spring_2027:
        exc = [x for x in exc if x.lower() not in _INTERN_EXCLUDE_TERMS]
    if inc and not spring_2027 and not contains_any(t, inc):
        return False
    if contains_any(t, exc):
        return False
    for rule in f.get("title_exclude_unless", []):  # non-finance engineering/trade titles only; a finance word keeps the role
        if contains_any(t, rule["terms"]) and not contains_any(t, rule["unless"]):
            return False
    if _BARE_HQ_RE.match(loc_folded):
        return False
    if _BARE_NON_US_CITY_RE.match(loc_folded):
        return False
    if _BARE_NON_US_CODE_RE.match(loc_folded):
        return False
    if _ONTARIO_RE.search(loc_folded):
        return False
    if _MEXICO_RE.search(loc_folded):
        return False
    if _DUBLIN_RE.search(loc_folded):
        return False
    if _ATHENS_RE.search(loc_folded):
        return False
    if _VANCOUVER_RE.search(loc_folded):
        return False
    if _COUNTRY_CODE_SITE_RE.match(loc_folded):
        return False
    if _TRAILING_NON_US_CC_RE.search(loc_folded):
        return False
    if _EY_NON_US_RE.match(loc_folded):
        return False
    if (_ISO3_DASH_RE.match(loc_folded) or _ISO2_SUBDIV_RE.match(loc_folded)
            or _CA_POSTAL_RE.search(job_loc) or _CA_PROVINCE_CODE_RE.search(job_loc)):
        return False
    if locs and not any(x in loc for x in locs):
        return False
    if contains_any(loc_folded, loc_exc):
        return False
    return True


def classify_sponsorship(text):
    """Blocked on language that rules out OPT too (citizenship, clearance, permanent authorization, OPT
    exclusion), "OPT Only" on plain no-sponsorship language, else Unclear. Never Allowed."""
    if not text:
        return "Unclear", "Description unavailable; not checked."
    normalized = _normalize_abbreviations(text)  # same length as `text` - offsets stay valid

    def evidence(m):
        s, e = max(0, m.start() - 50), min(len(text), m.end() + 50)
        return "..." + text[s:e].strip() + "..."
    for rx in HARD_BLOCK_RES:
        m = rx.search(normalized)
        if m:
            return "Blocked", evidence(m)
    first = None
    tag = ""
    for rx in BLOCK_RES:
        for m in rx.finditer(normalized):  # every match: the OPT list can follow a later sentence, not the first hit
            first = first or m
            window = normalized[max(0, m.start() - OPT_WINDOW_CHARS):m.end() + OPT_WINDOW_CHARS]
            for k in _OPT_WORD_RE.finditer(window):
                if _SIGNOFF_RE.search(window[max(0, k.start() - 300):k.start()]):
                    if re.search(r"stem\s*opt", window, re.I):
                        return "Blocked", evidence(m)  # refuses STEM OPT paperwork (I-983) too, so no OPT route
                    return "Blocked", evidence(m)  # 2026-10-19: refusing OPT paperwork excludes OPT, so Blocked
                elif k.group(0).lower() == "opt" and re.search(r"stem\s*$", window[max(0, k.start() - 8):k.start()], re.I):
                    tag = tag or "[no STEM OPT] "
                else:
                    return "Blocked", evidence(m)
    if first:
        if not tag and re.search(r"\bfuture\b", normalized[first.start():first.end() + 60], re.I):
            tag = "[now or future] "
        return "OPT Only", tag + evidence(first)
    return "Unclear", "No sponsorship language in the description."


_EXPERIENCE_YEARS_RE = re.compile(
    r"(?<![\d,.])(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*years?|"      # "3-5 years" / "3 to 5 years"
    r"(?<![\d,.])(\d{1,2})\s*\+\s*years?|"                                     # "5+ years"
    r"(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d{1,2})\s*years?",  # "minimum of 3 years"
    re.I,
)
_ENTRY_PHRASE_RE = re.compile(
    r"\bentry[- ]level\b(?!\s+(?:employees?|staff|to\s+(?:senior|executive|c-suite)))|\bnew grad(?:uate)?s?\b|\brecent graduate\b|\bcampus hire\b|"
    r"\bno (?:prior )?experience (?:required|necessary)\b",
    re.I,
)
_SENIOR_TITLE_RE = re.compile(r"\b(senior|sr\.?|vice president|vp|svp|principal|lead|staff)\b", re.I)
_MID_TITLE_RE = re.compile(r"\bassociate\b", re.I)
# Added after the two above so existing title-inferred values are unchanged ("Associate Director" stays 3).
_DIRECTOR_TITLE_RE = re.compile(r"\b(?:managing director|director|head of)\b", re.I)
_ENTRY_TITLE_RE = re.compile(
    r"\bintern(?:ship)?s?\b|\bsummer analyst\b|\btrainee\b|\bapprentice(?:ship)?\b|\bgraduate (?:program|scheme|analyst)\b|"
    r"\bentry[- ]level\b|\b(?:junior|jr\.?)\b", re.I)

_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_NUM = r"(\d{1,2}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_NUM_PAREN = r"(?:\s*\(\d{1,2}\))?"
# "3 years", "3+ yrs", "three (3) years", "3-5 years", "3 to 5 years", "five or more years", "5 years or more"
_STATED_YEARS_RE = re.compile(
    r"\b" + _NUM + _NUM_PAREN + r"(?:\s*(?:-|–|to)\s*" + _NUM + _NUM_PAREN + r")?"
    r"\s*(?:\+|\bplus\b)?\s*(?:or\s+more\s+)?[-\s]*(?:years?|yrs?)\b['’]?(?!\s+(?:old|ago)\b)",
    re.I)
_EXP_WORD_RE = re.compile(r"\bexperience[ds]?\b|\bexp\b", re.I)
# a number before "years" that cannot be a requirement: tenure of the employer, an age, a service award, a
# contract term ("within three years of hire") or fiscal/calendar years
_YEARS_NOT_REQ_AFTER_RE = re.compile(
    r"^['’]?\s*(?:(?:college|university|degree|bachelor|undergraduate|program|plan|period|term|contract|cycle|"
    r"horizon|track|accredited|institution|school|vision|strategy|roadmap|associate|master)\b|"
    r"of\s+(?:age|service|history|operations?|growth|business|profitab|unbroken|heritage|legacy|"
    r"success|innovation|tradition)|in\s+business|old|ago|from\s+(?:now|hire|the\s+date)|fiscal|calendar|"
    r"after|since|prior|post)", re.I)
_YEARS_NOT_REQ_BEFORE_RE = re.compile(
    r"(?:within|after|every|each|per|during|for\s+the\s+(?:next|past|last)|over\s+the\s+(?:next|past|last)|"
    r"next|past|last|previous|prior|fiscal|calendar|founded|established|since|for)\s+(?:the\s+)?(?:\w+\s+)?$", re.I)
_ORG_WORDS_RE = re.compile(
    r"\b(?:we|we've|our|firm|company|bank|organization|organisation|group|heritage|legacy|history|founded|"
    r"established|serving|providing|leader|leading)\b", re.I)
_DEGREE_WORD_RE = re.compile(r"degree|diploma|\bged\b|bachelor|master|\bmba\b|ph\.?d|doctorate|associate'?s|education", re.I)
_PREFERRED_NEAR_RE = re.compile(
    r"\bpreferred\b|\bdesired\b|\bdesirable\b|\ba plus\b|\bnice to have\b|\bbonus\b|\bideal(?:ly)?\b|\bbeneficial\b|"
    r"\ban? (?:advantage|asset)\b|\bhelpful\b|\bfavou?rable\b", re.I)
_PREF_HEADING_RE = re.compile(
    r"\b(?:preferred|desired|nice[- ]to[- ]have|bonus|ideal|additional|plus)\s+(?:qualifications?|skills?|experience|"
    r"requirements?|attributes|points)\b|\bbonus points\b|\bwhat would make you stand out\b|\bnice to haves?\b|"
    r"\bit'?s a plus\b|\bpreferred:", re.I)
_REQ_HEADING_RE = re.compile(
    r"\b(?:required|minimum|basic|mandatory)\s+(?:qualifications?|skills?|experience|requirements?|education)\b|"
    r"\byou'?ll need\b|\byou will need\b|\bmust have\b|\bwhat you'?ll bring\b|\bwe'?re looking for\b|"
    r"\bwhat we look for\b|\brequirements?:|\bqualifications?\s*:|\bminimum:", re.I)
# "Minimum Years of Work Experience: 3" (Workday-style structured field)
_STRUCTURED_YEARS_RE = re.compile(r"\bminimum years of (?:work |related |relevant )?experience\s*:\s*(\d{1,2})\b", re.I)


def _num_val(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _NUM_WORDS[tok]


_YEARS_MAX_BEFORE_RE = re.compile(r"(?:no more than|not more than|at most|up to|less than|fewer than|under|maximum of|a maximum of)\s+(?:about\s+)?$", re.I)


def _stated_experience_years(text):
    """Stated years-of-experience requirement the original regexes miss: number words, "(3)" parentheticals,
    ranges, "or more", bare "N years of ... experience", "N years' experience", structured fields. Returns
    (lower bound, snippet) or None. Conservative: the number must sit next to the word "experience", and
    employer tenure ("over 30 years"), ages, service awards, fiscal years, preferred-only years, and
    degree-or-experience alternatives are skipped (None = Unclear beats a wrong value)."""
    if not text:
        return None
    for m in _STRUCTURED_YEARS_RE.finditer(text):
        n = int(m.group(1))
        if n <= 20 and n > 0:
            return n, text[max(0, m.start() - 20):m.end() + 20].strip()
    for m in _STATED_YEARS_RE.finditer(text):
        try:
            lo = _num_val(m.group(1))
            hi = _num_val(m.group(2)) if m.group(2) else lo
        except KeyError:
            continue
        if max(lo, hi) > 20 or min(lo, hi) == 0 and not m.group(2):
            continue
        before = text[max(0, m.start() - 90):m.start()]
        sent_before = re.split(r"[.;!?]\s", before)[-1]
        after = text[m.end():m.end() + 100]
        if _YEARS_NOT_REQ_AFTER_RE.match(after) or _YEARS_NOT_REQ_BEFORE_RE.search(sent_before):
            continue
        if _YEARS_MAX_BEFORE_RE.search(sent_before):
            continue  # "no more than three years of experience" is a ceiling (an entry-level pitch), not a floor
        if re.search(r"(?:over|more than|nearly|almost|for|with)\s*$", sent_before, re.I) and _ORG_WORDS_RE.search(
                sent_before) and not re.search(r"\b(?:candidates?|applicants?|you|looking|seeking|require[sd]?|need)\b",
                                               sent_before, re.I):
            continue  # "a bank with more than 20 years of experience"
        # must be talking about experience: right after ("years of relevant experience") or a label right before
        after_sent = re.split(r"[.;!?]\s", after)[0]
        near_after = _EXP_WORD_RE.search(after_sent[:80])
        near_before = re.search(r"\bexperience\s*(?:required|level|needed)?\s*[:\-–]\s*(?:minimum\s+)?(?:of\s+)?"
                                r"(?:at least\s+|more than\s+|over\s+)?$", sent_before, re.I)
        if not (near_after or near_before):
            continue
        # preferred-only years are not a requirement
        if _PREFERRED_NEAR_RE.search(sent_before[-30:]) or _PREFERRED_NEAR_RE.search(after_sent[:100]):
            continue
        head_ctx = text[max(0, m.start() - 700):m.start()]
        pref = [h.end() for h in _PREF_HEADING_RE.finditer(head_ctx)]
        req = [h.end() for h in _REQ_HEADING_RE.finditer(head_ctx)]
        if pref and (not req or max(pref) > max(req)):
            continue
        # "Bachelor's degree or 4 years of experience" / "degree and/or 3 years": an alternative, not a floor
        alt_before = re.split(r"[.!?]\s", before)[-1]  # keeps ';' so "university; or at least 5 years" stays one clause
        if re.search(r"\b(?:or|and/or)\s+(?:(?:have|having|with|at least|minimum of|minimum|a|an|the equivalent of)\s+)*$",
                     alt_before, re.I) and _DEGREE_WORD_RE.search(alt_before):
            continue
        if re.match(r"[^.;]{0,50}?\bor\s+(?:a|an|the)?\s*(?:bachelor|master|mba|ph\.?d|doctorate|advanced degree|degree)", after, re.I):
            continue
        n = min(lo, hi)
        s, e = max(0, m.start() - 40), min(len(text), m.end() + 40)
        return n, text[s:e].strip()
    return None


def classify_experience(title, text):
    """Best-effort years-of-experience, keyed off the LOWER bound stated (e.g. "5+ years" or "3-5
    years" both key off 3/5, not an average or the upper end) since that's the number that actually
    gates whether someone can apply. Returns (years, evidence): years is an exact integer whenever
    the description states one, or 0 for explicit entry-level phrasing. The title is never used: no
    signal in the description is (None, ...) - Unclear, not assumed from the title or entry-level - same
    "never guess beyond the evidence" rule as sponsorship classification."""
    hay = text or ""
    m = _ENTRY_PHRASE_RE.search(hay)
    if m:
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        return 0, hay[s:e].strip()
    m = _EXPERIENCE_YEARS_RE.search(hay)
    if m:
        min_years = min(int(g) for g in m.groups() if g)
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        return min_years, hay[s:e].strip()
    stated = _stated_experience_years(hay)
    if stated:
        return stated
    return None, "No years-of-experience language found."


_SAL_CUR = r"(?:US\$|CA\$|C\$|CAD\s?\$|USD\s?\$|\$|£|€|USD|CAD|GBP|EUR)"
_SAL_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SAL_TAIL = r"(?:\s?(?:USD|CAD|GBP|EUR)\b)?"
_SALARY_RANGE_RE = re.compile(
    rf"(?<![\w,.])(?P<c1>{_SAL_CUR})?\s?(?P<n1>{_SAL_NUM})\s?(?P<k1>k\b)?(?P<t1>{_SAL_TAIL})(?:\s?/?\s?(?:hr|hrs|hour|yr|year)\b)?"
    rf"\s*(?P<sep>[-‐-―−]|\bto\b|\bthrough\b|\band\b)\s*"
    rf"(?P<c2>{_SAL_CUR})?\s?(?P<n2>{_SAL_NUM})\s?(?P<k2>k\b)?(?P<t2>{_SAL_TAIL})",
    re.I,
)
_SAL_LABEL = r"(?:\s*/?\s*(?:salary|pay|pay range|base pay|base salary|rate|compensation|range))*"


def _sal_amt(i):
    return (rf"(?P<c{i}>{_SAL_CUR})\s?(?P<n{i}>{_SAL_NUM})\s?(?P<k{i}>k\b)?(?P<t{i}>{_SAL_TAIL})"
            rf"\s*(?P<u{i}>/\s?(?:yr|year|hr|hour))?")


# "Min USD $82,400.00/Yr. Max USD $109,867.00/Yr." / "Minimum Salary: $155,000 per year Maximum Salary: $225,000 per year"
_SALARY_MINMAX_RE = re.compile(
    rf"\bmin(?:imum)?\b{_SAL_LABEL}[\s:]*" + _sal_amt(1) + r"[^\d$\u00a3\u20ac]{0,45}?\bmax(?:imum)?\b" + _SAL_LABEL + r"[\s:]*" + _sal_amt(2),
    re.I,
)
# "ranges from $200,000/year in our lowest geographic market up to $300,000" / "ranges from $138,771 and $208,156 USD"
_SALARY_FROMTO_RE = re.compile(
    r"\bfrom\s+" + _sal_amt(1) + r"[^\d$\u00a3\u20ac]{0,70}?\b(?:to|and)\s+" + _sal_amt(2),
    re.I,
)
_SALARY_SINGLE_RE = re.compile(
    rf"(?<![\w,.])(?P<c1>{_SAL_CUR})\s?(?P<n1>{_SAL_NUM})\s?(?P<k1>k\b)?(?P<t1>{_SAL_TAIL})", re.I)
_SALARY_CONTEXT_RE = re.compile(
    r"\b(?:salary|salaries|compensation|pay|base pay|base rate|pay range|pay ranges|pay rate|pay scale|pay band|"
    r"base range|total comp|shift range|wage|wages|hourly|remuneration|on[- ]target earnings|"
    r"(?:base|annual|starting|expected|estimated|current) (?:pay|salary)|range (?:is|for this|of)|"
    r"estimate of the (?:current )?range)\b", re.I)
# finance JDs are full of unrelated dollar figures ("$2B in assets", "$500M portfolio") - a nearby
# context word alone isn't always enough, so also reject anything sitting next to an asset-scale word
_SALARY_DISQUALIFY_RE = re.compile(
    r"\b(million|billion|trillion|aum|assets under management|in assets|portfolio|book of business|minimum wage|revenues?|valuation|funding|raised|deposits|loans?|transactions)\b", re.I)
# the pay figure itself must not be a scale shorthand ("$500M", "$2B", "$2.5bn")
_SALARY_SCALE_AFTER_RE = re.compile(r"^\s?(?:mm?|bn?|mn|tn|million|billion|trillion)\b", re.I)
# non-base amounts: a bonus / sign-on / match / equity figure is not the salary. Checked on the words directly
# before the amount ("sign-on bonus of $25,000", "401(k) match up to $5,000") and directly after it
# ("$25,000 sign-on bonus"), but "excluding bonus: $150,000" or "$150,000 plus bonus" still pass.
_SALARY_NONBASE_WORDS = (r"bonus|bonuses|sign[- ]?on|signing|retention|relocation|referral|stipend|401\s?\(?k\)?|"
                         r"match(?:ing)?|equity|stock|rsus?|commissions?|incentive|ote|on[- ]target|target earnings|"
                         r"variable|allowance|reimbursement|tuition|hsa|fsa|contribution|per diem|spot award|profit sharing")
_SALARY_NONBASE_BEFORE_RE = re.compile(rf"\b(?:{_SALARY_NONBASE_WORDS})(?![a-z])[^.;\d$]{{0,22}}$", re.I)
_SALARY_NONBASE_AFTER_RE = re.compile(rf"^\s{{0,2}}(?:usd\s*)?(?:annual\s+|cash\s+|one[- ]time\s+)?(?:{_SALARY_NONBASE_WORDS})", re.I)
_SALARY_EXCLUDING_RE = re.compile(r"\b(?:excluding|exclusive of|not including|without|before|plus|in addition to)\b", re.I)
_SALARY_PERIOD_RES = (
    ("hour", re.compile(r"\b(?:per\s+hour|an\s+hour|each\s+hour|per\s+hr|hourly)\b|/\s?(?:hr|hour|h)\b", re.I)),
    ("week", re.compile(r"\b(?:per\s+week|a\s+week|weekly)\b|/\s?(?:wk|week)\b", re.I)),
    ("month", re.compile(r"\b(?:per\s+month|a\s+month|monthly)\b|/\s?(?:mo|month)\b", re.I)),
    ("year", re.compile(r"\b(?:per\s+year|a\s+year|yearly|annual(?:ly)?|per\s+annum)\b|p\.a\.|/\s?(?:yr|year)\b", re.I)),
)
_SALARY_ANNUALIZE = {"hour": 2080, "week": 52, "month": 12, "year": 1}
# plausible magnitude per period (before annualising), so "$30 per week" or "$4,000 per hour" never survive
_SALARY_PERIOD_BOUNDS = {"hour": (10, 400), "week": (400, 12000), "month": (1700, 50000), "year": (20000, 600000)}
_SALARY_CUR_SYMBOL = {"$": "$", "usd": "$", "us": "$", "ca": "C$", "c": "C$", "cad": "C$", "gbp": "£", "£": "£",
                      "eur": "€", "€": "€"}


def _salary_num(digits, k_suffix):
    n = float(digits.replace(",", ""))
    return n * 1000 if k_suffix else n


def _salary_symbol(*marks):
    """Display symbol from the currency marks seen on a range ("USD $" counts as "$", "CAD" as "C$"); first
    informative one wins (so "$113,000 CAD - $163,000 CAD" is Canadian), a bare "$" meaning US dollars."""
    for mark in marks:
        mark = (mark or "").strip().lower()
        key = re.sub(r"\s*\$$", "", mark) if len(mark) > 1 else mark
        if key in _SALARY_CUR_SYMBOL and key != "$":
            return _SALARY_CUR_SYMBOL[key]
    return "$"


def _salary_period(hay, end, start):
    """Pay period named right after the amount ("per hour", "/yr", "USD Hourly") or, failing that, a short
    "hourly rate:" style lead-in; the earliest mention wins. None when unstated."""
    after = hay[end:end + 32]
    best = None
    for name, rx in _SALARY_PERIOD_RES:
        m = rx.search(after)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best:
        return best[1]
    before = hay[max(0, start - 30):start]
    for name in ("hour",):
        if re.search(r"\bhourly\b[^.]{0,22}$", before, re.I):
            return name
    for name, rx in (("month", re.compile(r"\bmonthly\b[^.]{0,22}$", re.I)), ("year", re.compile(r"\b(?:annual|yearly)\b[^.]{0,22}$", re.I))):
        if rx.search(before):
            return name
    return None


def _salary_fmt(x, sym):
    return f"{sym}{x:,.0f}" if abs(x - round(x)) < 0.005 else f"{sym}{x:,.2f}"


def _salary_k(x, sym):
    return f"{sym}{x / 1000:.1f}k".replace(".0k", "k")


_SALARY_MINWAGE_RE = re.compile(r"\bminimum wage\b[^.;\d$]{0,35}$", re.I)


def _salary_nonbase(hay, start, end):
    before = hay[max(0, start - 40):start]
    if _SALARY_MINWAGE_RE.search(before):  # statutory minimum wage notices, not this role's pay
        return True
    if _SALARY_NONBASE_BEFORE_RE.search(before) and not _SALARY_EXCLUDING_RE.search(before):
        return True
    return bool(_SALARY_NONBASE_AFTER_RE.search(hay[end:end + 30]))


def _salary_candidates(hay):
    """Every plausible pay figure in document order as dicts (kind range|single, start, end, lo, hi, period,
    sym), each already gated on a salary/compensation context word, the non-base/AUM guards and a plausible
    magnitude for its pay period."""
    out = []
    rejected = []  # spans of ranges refused for what they are (bonus, AUM, no context): their halves must not resurface as singles
    for is_minmax, m in [(True, x) for x in _SALARY_MINMAX_RE.finditer(hay)] + [(False, x) for x in _SALARY_FROMTO_RE.finditer(hay)]:
        lo, hi = _salary_num(m.group("n1"), m.group("k1")), _salary_num(m.group("n2"), m.group("k2"))
        unit = m.group("u1") or m.group("u2")
        period = ("hour" if re.search(r"h", unit, re.I) else "year") if unit else (_salary_period(hay, m.end(), m.start()) or "year")
        if lo > hi or not _salary_in_bounds(lo, hi, period):
            continue
        # a "/Yr." stated on Min/Max figures is structured pay and stands alone; every other pair needs the usual context
        if not (unit and is_minmax) and (not _SALARY_CONTEXT_RE.search(hay[max(0, m.start() - 120):m.end() + 20])
                         or _SALARY_DISQUALIFY_RE.search(hay[max(0, m.start() - 20):m.end() + 20])
                         or _SALARY_SCALE_AFTER_RE.search(hay[m.end():m.end() + 12])):
            rejected.append((m.start(), m.end()))
            continue
        out.append(dict(kind="range", start=m.start(), end=m.end(), lo=lo, hi=hi, period=period,
                        sym=_salary_symbol(m.group("c1"), m.group("c2"), m.group("t1"), m.group("t2"))))
    for m in _SALARY_RANGE_RE.finditer(hay):
        sep = m.group("sep").lower()
        c1, c2, t1, t2, k1, k2 = (m.group(g) for g in ("c1", "c2", "t1", "t2", "k1", "k2"))
        if sep == "and" and not re.search(r"\bbetween\s*$", hay[max(0, m.start() - 12):m.start()], re.I):
            continue
        if not (c1 or c2 or (t1 or "").strip() or (t2 or "").strip() or (k1 and k2)):
            continue  # "3 to 5 years", "2020 - 2024": a bare number pair is not money
        n1, n2 = m.group("n1"), m.group("n2")
        lo, hi = _salary_num(n1, k1 or k2 and float(n1.replace(",", "")) < 1000), _salary_num(n2, k2)
        if hi < lo:
            lo, hi = hi, lo
        period = _salary_period(hay, m.end(), m.start())
        if period is None:
            period = "year"  # no stated pay period: read as annual, so a small figure like "$31 - $36" fails the bounds check instead of being guessed as hourly
        if not _salary_in_bounds(lo, hi, period.rstrip("?")):
            continue
        if period == "year" and lo < 1000 and not (k1 or k2):
            continue
        context = hay[max(0, m.start() - 200):m.end() + 40]
        nearby = hay[max(0, m.start() - 20):m.end() + 20]
        # a location table continues the previous accepted range, whose context word may be out of reach by now
        continues = any(d["kind"] == "range" and 0 <= m.start() - d["end"] <= 250 for d in out)
        if (not (_SALARY_CONTEXT_RE.search(context) or continues) or _SALARY_DISQUALIFY_RE.search(nearby)
                or _SALARY_SCALE_AFTER_RE.search(hay[m.end():m.end() + 12]) or _salary_nonbase(hay, m.start(), m.end())):
            rejected.append((m.start(), m.end()))
            continue
        out.append(dict(kind="range", start=m.start(), end=m.end(), lo=lo, hi=hi, period=period,
                        sym=_salary_symbol(c1, c2, t1, t2)))
    out.sort(key=lambda d: d["start"])
    taken = [d for d in out if d["kind"] == "range"]
    for m in _SALARY_SINGLE_RE.finditer(hay):
        if any(d["start"] <= m.start() < d["end"] for d in taken) or any(a <= m.start() < b for a, b in rejected):
            continue  # already part of a range
        val = _salary_num(m.group("n1"), m.group("k1"))
        period = _salary_period(hay, m.end(), m.start()) or "year"
        if not _salary_in_bounds(val, val, period):
            continue
        if period == "year" and val < 1000:
            continue
        context = hay[max(0, m.start() - 60):m.end() + 15]
        nearby = hay[max(0, m.start() - 20):m.end() + 20]
        if not _SALARY_CONTEXT_RE.search(context) or _SALARY_DISQUALIFY_RE.search(nearby):
            continue
        if _SALARY_SCALE_AFTER_RE.search(hay[m.end():m.end() + 12]) or _salary_nonbase(hay, m.start(), m.end()):
            continue
        lead = hay[max(0, m.start() - 24):m.start()].lower()
        qual = "up to " if re.search(r"\bup to\W*$", lead) else "from " if re.search(r"\b(?:starting (?:at|from)|from|minimum(?: salary| base)?(?: of)?|min)\W*$", lead) else ""
        out.append(dict(kind="single", start=m.start(), end=m.end(), lo=val, hi=val, period=period, qual=qual,
                        sym=_salary_symbol(m.group("c1"), m.group("t1"))))
    out.sort(key=lambda d: d["start"])
    return out


def _salary_in_bounds(lo, hi, period):
    a, b = _SALARY_PERIOD_BOUNDS[period.rstrip("?")]
    return a <= lo <= b and a <= hi <= b


def _salary_render(lo, hi, period, sym, qual=""):
    """Display string. Annual keeps the plain "$120,000 - $150,000" form; other periods keep the stated rate and
    add the annualised estimate so the conversion is visible ("$45 - $55/hr (~$93.6k - $114.4k/yr)")."""
    p = period.rstrip("?")
    single = lo == hi
    if p == "year":  # whole dollars, as the original annual output always was
        lo, hi = int(lo), int(hi)
        return qual + (_salary_fmt(lo, sym) if single else f"{_salary_fmt(lo, sym)} - {_salary_fmt(hi, sym)}")
    unit = {"hour": "hr", "week": "wk", "month": "mo"}[p]
    f = _SALARY_ANNUALIZE[p]
    rate = qual + (_salary_fmt(lo, sym) if single else f"{_salary_fmt(lo, sym)} - {_salary_fmt(hi, sym)}") + f"/{unit}"
    ann = _salary_k(lo * f, sym) if single else f"{_salary_k(lo * f, sym)} - {_salary_k(hi * f, sym)}"
    assumed = ", assumed hourly" if period.endswith("?") else ""
    return f"{rate} (~{ann}/yr{assumed})"


def classify_salary(text):
    """Best-effort salary range from explicit description text, gated on BOTH a nearby salary/
    compensation context word and a plausible annual-salary magnitude ($20k-$600k) - a raw dollar
    regex alone would happily "find" a salary in "manages a $500M portfolio". Hourly / weekly / monthly
    pay is converted to an annual estimate and labelled as such; bonus, sign-on, match and equity figures
    and asset-scale amounts are rejected. When a posting lists several location ranges back to back the
    widest span is reported (evidence says so). Unclear when no confident match, never guessed from
    title/company alone."""
    hay = re.sub(r"\s+", " ", (text or "").replace(" ", " "))
    cands = _salary_candidates(hay)
    ranges = [d for d in cands if d["kind"] == "range"]
    if ranges:
        first = ranges[0]
        grp = [first]
        for d in ranges[1:]:  # location tables: same period and symbol, each right after the previous one
            if d["period"] == first["period"] and d["sym"] == first["sym"] and d["start"] - grp[-1]["end"] <= 400:
                grp.append(d)
        lo, hi = min(d["lo"] for d in grp), max(d["hi"] for d in grp)
        distinct = {(d["lo"], d["hi"]) for d in grp}  # a structured field often repeats the range in the text
        if len(distinct) > 1 and hi <= 2.2 * lo:
            note = f"[{len(distinct)} ranges listed, widest shown] "
        else:
            grp, lo, hi, note = [first], first["lo"], first["hi"], ""
        if first["period"] != "year" or note:
            note = (f"[converted from {first['period'].rstrip('?')}ly pay] " if first["period"] != "year" else "") + note
        s, e = grp[0]["start"], grp[-1]["end"]
        evidence = (note + hay[max(0, s - 40):min(len(hay), e + 40)].strip())[:400]
        return _salary_render(lo, hi, first["period"], first["sym"]), evidence
    for d in cands:
        evidence = hay[max(0, d["start"] - 40):min(len(hay), d["end"] + 40)].strip()
        if d["period"] != "year":
            evidence = f"[converted from {d['period'].rstrip('?')}ly pay] " + evidence
        return _salary_render(d["lo"], d["hi"], d["period"], d["sym"], d.get("qual", "")), evidence
    return "Unclear", ""


def keyword_hits(title, text, keywords):
    hay = f"{title} {text}".lower()
    return [k for k in keywords if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", hay)]


_REQ_NUMBER_RE = re.compile(r"\b[a-z]*-?\d{4,}(?:-\d+)*\b")  # also swallow a trailing -2/-3 repost suffix


def role_fingerprint(title, location):
    """Identity for 'is this the same role reposted under a new id' - not a job id, since a repost
    on Greenhouse/Lever usually mints a new one while Workday keeps the same req number, so id alone
    can't tell a repost from a genuinely different opening. Strips requisition-number-shaped tokens
    (e.g. "R249058-2") since those change on repost even when the role itself didn't."""
    def norm(s):
        s = _REQ_NUMBER_RE.sub("", (s or "").lower())
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    return f"{norm(title)}|{norm(location)}"


def _classify_ghosts(queue):
    """Flags evergreen/repeat-posting ("ghost") lineages: a role classified as "Likely" here has
    appeared GHOST_MIN_COUNT+ times under the same company+fingerprint, spanning GHOST_MIN_SPAN_DAYS+
    days, with each appearance starting only after the previous one went quiet (serial, not two
    genuinely concurrent openings for a common title). Deliberately never asserts the opposite - a
    role with no repost history yet is "Unclear", not "Clear", the same caution classify_sponsorship
    uses, since a role just hasn't had the chance to repeat yet doesn't mean it never will.

    This is unwindowed by design (checks the WHOLE queue, not just entries linked via repost_of/
    REPOST_WINDOW_DAYS) and recomputed queue-wide on every run, not classified once at creation like
    sponsorship - the signal only exists at the lineage level, and a chain that REPOST_WINDOW_DAYS
    fails to link (a very long gap between reposts) should still be visible here. It only ever
    tags entries red for review; nothing gets hidden or removed based on this."""
    groups = {}
    for e in queue:
        key = (e["company"], role_fingerprint(e["role"], e["location"]))
        groups.setdefault(key, []).append(e)

    for entries in groups.values():
        for e in entries:
            e["ghost_status"], e["ghost_evidence"] = "Unclear", ""
        if len(entries) < GHOST_MIN_COUNT:
            continue
        entries.sort(key=lambda e: e.get("first_seen") or "")
        serial = True
        for prev, cur in zip(entries, entries[1:]):
            boundary = prev.get("closed_on") or prev.get("last_seen_live") or prev.get("first_seen") or ""
            if not boundary or (cur.get("first_seen") or "") < boundary:
                serial = False
                break
        if not serial:
            continue
        first_seen, last_seen = entries[0].get("first_seen") or "", entries[-1].get("first_seen") or ""
        try:
            span = (date.fromisoformat(last_seen) - date.fromisoformat(first_seen)).days
        except ValueError:
            continue
        if span < GHOST_MIN_SPAN_DAYS:
            continue
        evidence = (f"posted {len(entries)}x since {first_seen} (span {span}d), "
                    f"most recently {last_seen}")
        for e in entries:
            e["ghost_status"], e["ghost_evidence"] = "Likely", evidence


REVERIFY_MAX_PER_RUN = 300
REVERIFY_WORKERS = 4


REFRESH_MIN_JUMP_DAYS = 2  # startDate drifts by one day at Workday's boundary; a 2+ day jump is a real renewal
REFRESH_MAX_PER_RUN = 100
_refresh_candidates = []


def _note_possible_refresh(e, c, listed):
    """Queues a live Workday entry whose bulk-listing age (relative postedOn text) is 2+ days newer than the
    stored posted date: the employer renewed the same requisition in place (same id and URL), which the
    new-id repost detection never sees. Confirmed against the detail endpoint in _apply_refreshed_dates."""
    stored = e.get("posted") or ""
    if not listed or not stored or e.get("closed_on") or e.get("posted_est"):
        return
    gap = (date.fromisoformat(listed) - date.fromisoformat(stored)).days
    if gap < REFRESH_MIN_JUMP_DAYS:
        return
    site = e.get("site") or c["site"]
    if isinstance(site, list):
        site = site[0]
    _refresh_candidates.append((e, c, e["id"].split(":", 1)[1], site))


def _apply_refreshed_dates(queue=None):
    """Confirms each queued candidate with the detail endpoint's exact startDate and, when it is 2+ days later
    than the stored date, moves `posted` to the latest date and records it as a repost (count + reposted_on)."""
    todo, moved = _refresh_candidates[:REFRESH_MAX_PER_RUN], 0
    del _refresh_candidates[:]

    def check(item):
        e, c, job_id, site = item
        try:
            return _workday_refresh_posted(c, job_id, site)
        except Exception:
            return None
        finally:
            time.sleep(0.3)

    with ThreadPoolExecutor(REVERIFY_WORKERS) as ex:
        results = list(ex.map(check, todo))
    for (e, _c, _j, _s), res in zip(todo, results):
        if not res or not res[0] or not e.get("posted"):
            continue
        new, src = res
        if (date.fromisoformat(new) - date.fromisoformat(e["posted"])).days >= REFRESH_MIN_JUMP_DAYS:
            e["posted_before_refresh"] = e["posted"]
            e["posted"], e["posted_source"], e["posted_reverified"] = new, src, True
            e["repost_count"] = e.get("repost_count", 0) + 1
            e["reposted_on"] = new
            moved += 1
    if todo:
        print(f"refresh: checked {len(todo)} renewed-looking Workday postings, moved {moved} to the newer date")
    return moved


def _reverify_recent_posted(queue, cfg):
    """Re-checks posted/posted_source once for each Workday entry first_seen between
    REVERIFY_MIN_AGE_DAYS and REVERIFY_MAX_AGE_DAYS ago - startDate can drift by a day depending on
    exactly when it's queried relative to Workday's own day boundary (see _workday_detail_posted),
    so a value captured right at first sighting isn't fully trustworthy until re-observed a day or
    two later. Marks every eligible entry posted_reverified=True whether or not the value actually
    changed, so each entry is only ever re-checked once - not every run for its whole eligible
    window, which would multiply this into dozens of redundant refetches per entry."""
    by_company = {c["name"]: c for c in cfg["companies"]}
    today = date.today()
    lo = (today - timedelta(days=REVERIFY_MAX_AGE_DAYS)).isoformat()
    hi = (today - timedelta(days=REVERIFY_MIN_AGE_DAYS)).isoformat()
    todo = []
    for e in queue:
        if e.get("posted_reverified") or e.get("closed_on"):
            continue
        first_seen = e.get("first_seen") or ""
        if not (lo <= first_seen <= hi):
            continue
        c = by_company.get(e["company"])
        if not c or c.get("ats") != "workday":
            continue
        site = e.get("site") or c["site"]
        if isinstance(site, list):
            site = site[0]  # pre-existing entry with no stored site - best-effort on a multi-site company
        todo.append((e, c, e["id"].split(":", 1)[1], site))
    todo = todo[:REVERIFY_MAX_PER_RUN]  # whole-board reads queued thousands at once; the rest wait for later runs
    changed = 0
    checked = len(todo)

    def refresh(item):
        e, c, job_id, site = item
        try:
            return _workday_refresh_posted(c, job_id, site)
        except Exception:
            return None  # likely closed since first_seen - the separate liveness check handles that
        finally:
            time.sleep(0.3)  # Workday 429s on bursts - see describe_pool's own comment in run()

    with ThreadPoolExecutor(REVERIFY_WORKERS) as ex:
        results = list(ex.map(refresh, todo))
    for (e, _c, _j, _s), res in zip(todo, results):
        if res is None:
            continue
        posted, posted_source = res
        e["posted_reverified"] = True
        if posted and posted != e.get("posted"):
            e["posted"], e["posted_source"] = posted, posted_source
            changed += 1
    if checked:
        print(f"reverify: checked {checked} recent Workday postings, corrected {changed}")
    return changed


def _retry_enrichment(queue, cfg, keywords, dry=False):
    """Retries what failed at intake, a few per run and at most once a day per entry: (1) description fetches
    that errored, which left sponsorship/experience/salary unchecked, and (2) undated entries of the ATSs in
    DETAIL_DATED_ATS (their detail call carries the date), which fall outside the one-shot 1-to-3-day date recheck above. Success reclassifies the entry."""
    by_company = {c["name"]: c for c in cfg["companies"]}
    today = date.today().isoformat()
    budget, fixed = ENRICH_RETRY_BUDGET, 0
    for e in queue:
        if budget <= 0:
            break
        if e.get("closed_on") or e.get("enrich_next", "") > today or e.get("enrich_attempts", 0) >= ENRICH_MAX_ATTEMPTS:
            continue
        c = by_company.get(e["company"])
        if not c:
            continue
        desc_failed = e.get("description_status") == "failed"
        undated = c.get("ats") in DETAIL_DATED_ATS and not e.get("posted")
        if not (desc_failed or undated):
            continue
        budget -= 1
        e["enrich_attempts"] = e.get("enrich_attempts", 0) + 1
        e["enrich_next"] = (date.today() + timedelta(days=1)).isoformat()
        job = dict(id=e["id"].split(":", 1)[1], url=e["link"], site=e.get("site"), title=e["role"], location=e["location"])
        try:
            text, posted, posted_source = describe(c, job)
        except Exception:  # noqa: BLE001  still failing: try again tomorrow, up to the attempt limit
            continue
        finally:
            if c.get("ats") == "workday":
                time.sleep(0.3)  # Workday 429s on bursts
        if job["location"] != e["location"]:
            e["location"] = job["location"]  # describe() replaced "N Locations (city)" with every site
            fixed += 1
        if posted and not e.get("posted"):
            e["posted"], e["posted_source"] = posted, posted_source
            fixed += 1
        if desc_failed and text:
            e["sponsorship_status"], e["sponsorship_evidence"] = classify_sponsorship(text)
            e["experience"], e["experience_evidence"] = classify_experience(e["role"], text)
            e["salary"], e["salary_evidence"] = classify_salary(text)
            e["kw_hits"] = keyword_hits(e["role"], text, keywords)
            e["description_status"] = "ok"
            if e["state"] == "new" and e["sponsorship_status"] == "Blocked":
                e["state"] = "auto_blocked"
            elif e["state"] == "new" and e["sponsorship_status"] == "OPT Only":
                e["state"] = "opt_only"
            fixed += 1
    if fixed:
        print(f"retry: recovered {fixed} descriptions or posted dates")
    return fixed


ID_DATE_MIN_ANCHORS = 6    # fewest dated neighbours before the id-to-date relation is trusted at all
ID_DATE_MIN_SPEARMAN = 0.8  # rank correlation of numeric id vs date the anchors must show


def _spearman(xs, ys):
    """Spearman rank correlation (average ranks for ties); 0.0 when either side is constant."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out, i = [0.0] * len(v), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / den if den else 0.0


def estimate_posted_from_id(job_id, first_seen, anchors):
    """Approximate posting date (ISO) of a job with numeric `job_id` whose first_seen is the company's seed run, by
    interpolating its id between the nearest lower and higher ids in `anchors` ([(numeric id, ISO date)], dates the
    caller trusts). "" when no estimate is justified: non-numeric id, id already an anchor, a missing neighbour
    (a single bound is not an interpolation; the lower one alone is used only when it predates first_seen), fewer than
    ID_DATE_MIN_ANCHORS anchors, an id-vs-date rank correlation under ID_DATE_MIN_SPEARMAN, or neighbours whose dates
    run backwards. The estimate never exceeds first_seen: the job was already live at the seed run."""
    if not str(job_id).isdigit() or not first_seen:
        return ""
    jid = int(job_id)
    pts = sorted({(int(i), date.fromisoformat(d[:10]).toordinal()) for i, d in anchors if str(i).isdigit() and d})
    if len(pts) < ID_DATE_MIN_ANCHORS or any(i == jid for i, _ in pts):
        return ""
    if _spearman([i for i, _ in pts], [d for _, d in pts]) < ID_DATE_MIN_SPEARMAN:
        return ""
    lo = max((p for p in pts if p[0] < jid), default=None)
    hi = min((p for p in pts if p[0] > jid), default=None)
    cap = date.fromisoformat(first_seen[:10]).toordinal()
    if lo and hi:
        if lo[1] > hi[1]:
            return ""
        est = lo[1] + round((hi[1] - lo[1]) * (jid - lo[0]) / (hi[0] - lo[0]))
    elif lo and lo[1] <= cap:
        est = lo[1]
    else:
        return ""
    return date.fromordinal(min(est, cap)).isoformat()


def _fill_dates_from_id(queue, cfg):
    """Gives undated seed-run entries of companies with `date_from_id: true` an approximate posted date from
    estimate_posted_from_id (marked posted_source "approx" and posted_est so a later run may refine it). Anchors are
    the company's rows with a real or approx posted date, plus rows first seen after the seed run, whose first_seen
    is accurate to about a day. Never overrides a posted value that is not one of its own estimates."""
    filled = 0
    for c in cfg["companies"]:
        if not c.get("date_from_id"):
            continue
        rows = [(e, e["id"].split(":", 1)[1]) for e in queue if e["company"] == c["name"]]
        anchors = []
        for e, jid in rows:
            if e.get("posted") and not e.get("posted_est"):
                anchors.append((jid, e["posted"]))
            elif not e.get("first_run") and e.get("first_seen"):
                anchors.append((jid, e["first_seen"]))
        for e, jid in rows:
            if not e.get("first_run") or (e.get("posted") and not e.get("posted_est")):
                continue
            est = estimate_posted_from_id(jid, e.get("first_seen"), anchors)
            if est and est != e.get("posted"):
                e["posted"], e["posted_source"], e["posted_est"] = est, "approx", True
                filled += 1
    if filled:
        print(f"date_from_id: estimated {filled} posted dates from neighbouring ids")
    return filled


# --------------------------------------------------------------------- notify

def notify(cfg, entry, dry):
    """Returns True when the alert was delivered (or printed), False when it failed or was skipped."""
    line = (f"{entry['company']}: {entry['role']} ({entry['location'] or 'n/a'}) | "
            f"sponsorship {entry['sponsorship_status']}"
            + (f" | {', '.join(entry['kw_hits'][:5])}" if entry["kw_hits"] else ""))
    kind = cfg.get("type", "stdout")
    if dry or kind == "stdout":
        print(("[dry-run] " if dry else "[alert] ") + line + "\n    " + entry["link"])
        return True
    topic = cfg.get("topic") or os.environ.get("NTFY_TOPIC")
    if kind == "ntfy" and not topic:
        print(f"! notify skipped: no ntfy topic (set NTFY_TOPIC env var or cfg.notify.topic): {line}", file=sys.stderr)
        return False
    try:
        if kind == "ntfy":
            server = cfg.get("server", "https://ntfy.sh").rstrip("/")
            tracker_url = cfg.get("tracker_url")
            hdr = {"Title": f"New job: {entry['company']}".encode("ascii", "ignore").decode(), "Click": entry["link"]}
            if tracker_url:  # secondary button - keeps the main tap on the job link, adds a way into the full queue
                hdr["Actions"] = f"view, Open Jobs Tracker, {tracker_url}"
            http(f"{server}/{topic}", data=line.encode(), headers=hdr, raw=True)
        elif kind == "telegram":
            http(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                 data={"chat_id": cfg["chat_id"], "text": f"{line}\n{entry['link']}"})
    except Exception as e:  # an alert failure must not lose the queue entry
        print(f"! notify failed: {e}", file=sys.stderr)
        return False
    return True


# ------------------------------------------------------------------- commands

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def cmd_init():
    HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = HOME / "config.json"
    if cfg_path.exists():
        print(f"{cfg_path} already exists; not overwriting.")
        return
    example = REPO_DIR / "config.example.json"
    cfg = json.loads(example.read_text(encoding="utf-8"))
    atomic_json.write(str(cfg_path), cfg)
    print(f"Wrote {cfg_path}\nSet the NTFY_TOPIC env var (or GitHub Actions secret) to your ntfy.sh topic - "
          f"it is deliberately not stored in config.json since this repo is public.")


def cmd_queue(args):
    q = load_json(HOME / "queue.json", [])
    if not args.all:
        q = [e for e in q if e["state"] == "new" and not e.get("closed_on")]
    if args.days is not None:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()
        # postings with no parseable date are kept, not dropped - "unknown" isn't the same as "old"
        q = [e for e in q if not e.get("posted") or e["posted"] >= cutoff]
    def sort_key(e):
        posted = e.get("posted") or ""
        try:
            ordinal = date.fromisoformat(posted).toordinal()
        except ValueError:
            # no posted date: rank by when we first saw it instead of letting every undated
            # posting tie and fall through entirely to the keyword-count tiebreak
            try:
                ordinal = date.fromisoformat(e.get("first_seen") or "").toordinal()
            except ValueError:
                ordinal = 0
        return (-ordinal, -len(e["kw_hits"]), e["company"])

    q = sorted(q, key=sort_key)
    if args.json:
        print(json.dumps(q, indent=1))
        return
    if args.html:
        updated_at = (datetime.fromtimestamp((HOME / "queue.json").stat().st_mtime, tz=ZoneInfo("America/New_York"))
                      if (HOME / "queue.json").exists() else None)
        # public page: never expose applied/shortlisted/dismissed status; opt_only and auto_blocked are properties of the posting, not decisions
        q = [e for e in q if e["state"] in ("new", "opt_only", "auto_blocked")]
        write_queue_html(q, args.html, updated_at)
        print(f"wrote {len(q)} entries to {args.html}")
        return
    for e in q:
        print(f"{e['id']}  {e['company']}: {e['role']} ({e['location']}) posted {posted_label(e)} "
              f"[{e['state']}, spons {e['sponsorship_status']}, exp {e.get('experience', 'Unclear')}, "
              f"salary {e.get('salary', 'Unclear')}, kw {', '.join(e['kw_hits']) or 'none'}]\n    {e['link']}")
    print(f"{len(q)} queue entr{'y' if len(q) == 1 else 'ies'}.")


def posted_label(e):
    posted = e.get("posted") or ""
    # "~" flags a date derived from Workday's relative "Posted N Days Ago" text rather than an
    # exact timestamp (see describe()/_workday_detail_posted's posted_source) - it's not wrong,
    # but it's a day-count bucket, not a real recorded date, so it shouldn't look as certain as one.
    prefix = "~" if e.get("posted_source") == "approx" else ""
    if posted == date.today().isoformat():
        return prefix + "today"
    if posted:
        return prefix + posted
    first_seen = e.get("first_seen") or ""
    return f"seen {first_seen[5:]}" if first_seen else "unknown"  # MM-DD, no real posted date to trust


# states shown collapsed below the main "new" table, in this order; any other state found (e.g.
# a custom state from a hand-edit) is appended after these
DATE_FILTER_DAYS = [0, 1, 3, 7, 14, 30, 45, 60, 90, 180, 365, 730]
_SECONDARY_STATE_ORDER = ["shortlisted", "applied", "opt_only", "auto_blocked", "dismissed"]
_STATE_LABELS = {"opt_only": "No H1B (no employer sponsorship stated)", "auto_blocked": "Blocked (citizenship, clearance or STEM OPT excluded)"}


# Cloudflare Worker that returns a posting's full JD text (see worker/README.md). Blank = the Copy JD
# button reports "not configured" instead of failing silently.
JD_PROXY_URL = "https://job-scout-jd-proxy.yazad-jobscout.workers.dev"

ACTIONS_TD = '<td class="act"><button type="button" class="skip-btn copy-act">Copy JD</button></td>'

HIDE_COMPANY = "JPMorgan Chase"

# Title-only, word-boundary matched in the page script; unticked = no restriction.
_FINE_ROLE_CATEGORIES = {
    "Treasury/ALM/Liquidity": [
        "treasury", "alm", "asset liability management", "asset/liability management", "asset-liability",
        "liquidity risk", "liquidity management", "lcr", "nsfr", "liquidity",
    ],
    "FP&A/Financial Analysis": [
        "fp&a", "fp & a", "financial planning and analysis", "financial planning & analysis", "financial analyst",
        "financial analysis", "finance analyst", "finance manager", "finance business partner", "business finance",
        "budget", "budgeting", "financial modeler", "financial modeling", "planning and analysis",
        "planning & analysis", "financial planning", "finance and business management",
        "finance & business management", "portfolio finance", "finance lead", "forecasting", "finance control",
        "finance analytics", "finance decision", "capital allocation", "capital mgmt", "capital controls",
        "capital governance", "capital reporting", "capital optimization",
    ],
    "Credit Risk": [
        "credit risk", "credit analyst", "credit analysis", "counterparty credit risk", "credit portfolio",
    ],
    "Fixed Income/Trading/Markets": [
        "fixed income", "trader", "trading", "trade support", "derivatives", "global markets", "equity derivatives",
        "swap", "swaps", "fx", "foreign exchange", "rates trading", "commodity trading", "commodities trading",
        "sales and trading", "sales & trading", "equity structuring", "structuring desk", "structurer",
        "prime brokerage", "market making", "xva", "us rates", "swapclear", "prime services", "secured financing",
        "equity financing",
    ],
    "Operational Risk": [
        "operational risk", "business risk", "internal controls", "enterprise risk", "risk governance",
        "governance risk",
    ],
    "Model & Quantitative Risk": [
        "quantitative risk", "quantitative analyst", "quant analyst", "quantitative analytics",
        "model risk", "model validation", "model risk management",
    ],
    "Capital Planning": [
        "capital planning", "resolution planning", "ccar", "icaap", "basel", "rwa", "pillar 3", "capital adequacy",
        "stress testing", "regulatory reporting",
    ],
    "Market Risk": [
        "market risk", "value at risk", "var", "trading risk",
    ],
    "Investment Banking/M&A": [
        "capital markets", "investment banking", "equity capital markets", "corporate finance",
        "corporate development", "m&a", "mergers", "mergers and acquisitions", "restructuring", "financial sponsor",
        "financial sponsors", "coverage group", "industry coverage", "ib", "ecm", "dcm", "debt capital markets",
        "leveraged finance associate", "leveraged finance analyst", "leveraged finance origination",
        "capital markets leveraged finance", "investment banking, leveraged finance",
        "investment banking & capital markets, leveraged finance", "capital solutions", "debt capital market",
        "capital market group", "capital structure advisory", "corporate coverage", "financial institutions group",
        "global financial institutions", "corporate advisory", "financial advisory", "strategic advisory",
        "fig advisory",
    ],
    "Risk Management (General)": [
        "risk management", "risk compliance", "risk controls", "risk control", "security risk",
        "information security", "third party risk", "third-party risk", "technology risk", "financial risk",
        "risk advisory", "risk analyst", "risk manager", "risk officer", "risk advisor", "risk consultant",
        "risk lead", "risk partner", "risk associate", "risk analytics", "risk engineer", "risk reporting",
        "risk and controls", "risk & controls", "risk and control", "risk & control", "risk strategy", "risk policy",
        "portfolio risk", "investment risk", "investments risk", "counterparty risk", "global markets risk",
        "credit policy", "risk oversight", "risk operations", "tech risk", "technology & data risk",
        "technology and data risk", "data risk", "risk and governance", "risk & governance", "risk director",
        "risk specialist", "risk senior", "chief risk officer", "risk/compliance", "risk and compliance",
        "risk & compliance", "business controls", "first line risk", "business line risk", "control officer",
        "capital risk", "risk analysis", "risk program", "risk policy management", "risk / policy management",
        "risk/policy management", "risk business partner", "risk mitigation", "risk monitoring", "risk assessment",
        "integrated risk", "emerging risk", "clearing risk", "resilience risk", "climate risk",
        "environmental and social risk", "risk identification", "ai risk", "ai validation", "ai model validation",
    ],
    "Portfolio/Asset Management & Research": [
        "portfolio management", "asset management", "investment management", "portfolio analytics",
        "investment operations", "equity research", "portfolio manager", "portfolio managers", "portfolio analyst",
        "portfolio specialist", "portfolio associate", "portfolio analysts", "portfolio construction",
        "portfolio strategy", "portfolio strategist", "portfolio consultant", "portfolio implementation",
        "portfolio solutions", "portfolio director", "portfolio mgr", "portfolio research", "research analyst",
        "research associate", "investment analyst", "investment associate", "credit research", "global research",
        "fundamental", "investment research", "research analysts", "quantitative research", "investment professional",
    ],
    "Banking/Lending/Underwriting": [
        "underwriting", "commercial portfolio", "commercial banking", "commercial credit", "commercial lending",
        "commercial loan", "relationship manager", "relationship banking", "relationship lending", "corporate banker",
        "portfolio banker", "client banker", "relationship banker", "commercial banker", "business banker",
        "community banker", "corporate banking", "middle market", "market manager", "credit officer",
        "credit products", "credit delivery", "credit administration", "credit underwriter", "loan officer",
        "loan portfolio", "loan operations", "loan servicing", "loans", "workout", "special assets",
        "loss mitigation", "collections", "dealer funding", "asset based lending", "asset-based lending",
        "leveraged lending", "business banking", "lending specialist", "lending group", "lending officer",
        "lending associate", "lending analyst", "lending advisor", "home lending", "consumer lending",
        "direct lending", "branch", "banking center", "financial center", "personal banker", "teller",
        "consumer banking", "retail banking", "retail management", "retail loan", "retail credit", "retail lending",
        "retail operations", "retail application", "retail and small business", "retail & small business",
        "home loan", "home equity", "mortgage loan", "mortgage lending", "mortgage servicing", "mortgage banker",
        "mortgage business", "deposit operations", "deposit ops", "deposit admin", "mobile deposit",
        "deposit acquisition", "deposit products", "auto lending", "auto finance", "closing specialist",
    ],
    "Private Markets/Real Estate/Structured Finance": [
        "real estate", "commercial real estate", "real assets", "private equity", "private credit", "private capital",
        "capital advisory", "structured finance", "structured credit", "structured products", "securitized",
        "securitization", "asset backed", "asset-backed", "asset based", "mortgage backed", "mortgage-backed",
        "backed securities", "clo", "clos", "abs", "rmbs", "cmbs", "abf", "energy", "power & utilities",
        "power and utilities", "power finance", "power generation", "power infrastructure", "capital projects",
        "capital project", "construction lending", "construction finance", "construction loan",
        "construction management", "construction engineer", "project finance", "renewable", "utilities",
        "oil and gas", "infrastructure finance", "infrastructure & project finance",
    ],
    "Wealth/Trust": [
        "wealth management", "registered client", "private banking", "wealth advisor", "wealth adviser",
        "wealth planning", "wealth planner", "private wealth", "wealth solutions", "wealth strategies",
        "wealth associate", "global wealth", "wealth studio", "wealth mgmt", "wealth services", "wealth analyst",
        "trust administrator", "trust officer", "trust portfolio manager", "trust and estates", "trust & estates",
        "trust company", "trust services", "fiduciary", "estate trust", "estate planning", "financial advisor",
        "financial consultant", "private client", "philanthropic", "private bank", "family office",
        "investment specialist", "ocio", "ayco", "financial planner",
    ],
    "Accounting/Controllership/Tax": [
        "accounting", "accountant", "controller", "controllers", "controllership", "financial reporting",
        "external reporting", "management reporting", "tax", "general ledger", "product control",
        "product controller", "fund accounting", "consolidation",
    ],
    "Compliance/Audit": [
        "compliance", "aml", "bsa", "kyc", "financial crime", "financial crimes", "anti-money laundering",
        "sanctions", "fraud", "surveillance", "regulatory affairs", "cdd", "audit", "auditor", "internal audit",
        "risk assurance", "assurance associate", "compliance assurance", "sox",
    ],
    "Quant/Modeling/Pricing": [
        "quant", "quantitative", "financial engineering", "valuation", "valuations", "modeler", "model development",
        "pricing analytics", "xva", "capital modeling",
    ],
    "Operations/Payments": [
        "operations associate", "operations analyst", "operations specialist", "operations manager",
        "operations lead", "operations client", "operations senior", "operations officer", "operations coordinator",
        "securities operations", "derivatives operations", "derivative operations", "investor services",
        "asset services", "asset servicing", "client order management", "client reporting", "letters of credit",
        "documentation negotiator", "commodities documentation", "operations expert", "clearing", "loan funding",
        "loan booking", "funding specialist", "loan management", "credit facility management", "portfolio reporting",
        "core operations", "deposit operations", "service operations", "conversion operations",
        "banking services operations", "client operations", "fund operations", "middle office", "back office",
        "settlements", "settlement", "servicing", "securities processing", "processing", "onboarding",
        "client service", "client services", "fund services", "corporate actions", "corporate action", "custody",
        "reconciliation", "document control", "trade operations", "payment operations", "announcement capture",
        "collateral", "process manager", "process associate", "payments", "payment", "cash management",
        "merchant services", "merchant onboarding", "merchant acquiring", "merchant solutions", "commercial card",
        "card sales", "card solutions", "fintech", "treasury services", "liquidity solutions",
    ],
    "Technology/Data": [
        "software", "developer", "data engineer", "data analytics", "data analyst", "data product", "data science",
        "data scientist", "data governance", "data management", "data architect", "data and ai", "data & ai",
        "data and analytics", "data & analytics", "data platform", "machine learning", "cloud engineer",
        "cloud architect", "cloud platform", "cloud security", "solutions architect", "solution architect",
        "enterprise architect", "technical architect", "ai architect", "platform architect", "application architect",
        "cyber", "cybersecurity", "devops", "sre", "network engineer", "systems administrator", "systems engineer",
        "salesforce", "technologist", "database", "etl", "information technology", "it analyst",
        "it business analyst", "it infrastructure", "it service desk", "it support", "it operations",
        "technology analyst", "technology associate", "technology director", "technology operations",
        "technology infrastructure", "technology rotation", "technology solutions", "technology support",
        "technology management", "technology integration", "domain architect", "delivery architect", "ai solutions",
        "ai engineer", "ai engineering", "genai", "gen ai", "generative ai", "applied ai", "ai/ml", "ai automation",
        "ai platform", "advanced analytics", "business intelligence", "business analytics", "analytics solutions",
        "decision science", "product analytics", "healthcare analytics", "data protection", "data quality",
        "data visualization", "data exchange", "data solutions", "data validation", "data initiatives",
        "analytics engineer", "analytics engineering", "ml engineer", "platform engineer", "front end engineer",
        "mobile engineer", "qa engineer", "release engineer", "production engineer", "engineering manager",
        "director of engineering", "director, engineering", "associate director engineering",
        "associate director, engineering",
    ],
    "Product/Program/Strategy": [
        "product manager", "product management", "product owner", "product delivery", "product analyst",
        "product lead", "product strategy", "product development", "product specialist", "product director",
        "product marketing", "product operations", "product associate", "corporate strategy", "business strategy",
        "strategy and operations", "strategy & operations", "strategy and insights", "strategy & insights",
        "strategy and business development", "strategy and planning", "strategy & planning", "growth strategy",
        "digital strategy", "finance & strategy", "finance and strategy", "strategy specialist",
        "strategy enablement", "bank strategy", "strategy, associate", "strategy, senior associate",
        "management & strategy", "leadership and strategy", "strategic advisory", "strategic initiatives",
        "strategic planning", "strategic change", "strategic partnerships", "strategic finance",
        "strategic intelligence", "senior consultant", "associate consultant", "principal consultant",
        "business consultant", "consulting", "management consulting", "engagement manager", "transformation",
        "chief of staff", "business analyst", "business analysis", "business systems analyst", "program manager",
        "program management", "project manager", "project management", "pmo", "change management",
        "process improvement", "business solutions", "business management", "implementation manager",
        "implementation consultant", "implementation associate", "implementation lead", "implementation officer",
        "implementation specialist", "implementation analyst", "program control", "program controls",
        "business process",
    ],
    "Sales/Business Development": [
        "sales", "business development", "account executive", "client solutions", "account manager", "client success",
        "wholesaler", "distribution sales", "investor relations", "consultant relations", "client advisor",
        "client partner", "solutions specialist", "institutional client", "client specialist", "capital introduction",
        "capital formation", "client coverage", "national accounts", "client engagement",
    ],
    "Insurance/Actuarial": [
        "insurance", "actuary", "actuarial", "claims", "reinsurance", "annuity", "annuities", "employee benefits",
        "health & benefits", "retirement income", "absence consultant", "benefits consultant", "associate, brokerage",
        "property casualty", "production underwriter", "personal lines", "commercial lines", "excess & surplus",
        "excess and surplus",
    ],
    "Early Careers/Internships": [
        "intern", "internship", "summer analyst", "summer associate", "graduate", "analyst program",
        "development program", "accelerated career track", "trainee", "rotation", "rotational", "early careers",
        "early career", "talent program", "campus", "apprentice", "2027",
    ],
}

# Chips shown on the page: neighbouring fine categories above merged into one chip (merged label -> members).
# Quant/Modeling/Pricing stays separate because its exclusions ("valuation control", "controllers") would
# wrongly drop a "Quantitative Analyst, Valuation Control" title from the model/quant risk chip.
CHIP_MERGES = {
    "Credit Risk/Lending": ["Credit Risk", "Banking/Lending/Underwriting"],
    "Risk/Compliance/Op Risk": ["Operational Risk", "Risk Management (General)", "Compliance/Audit"],
    "Portfolio/Asset Mgmt/Wealth": ["Portfolio/Asset Management & Research", "Wealth/Trust"],
    "Other Business Roles": ["Operations/Payments", "Technology/Data", "Product/Program/Strategy",
                             "Sales/Business Development", "Insurance/Actuarial", "Early Careers/Internships"],
}


def _merge_chips(fine, merges):
    to_new = {m: new for new, members in merges.items() for m in members}
    out = {}
    for label, terms in fine.items():
        key = to_new.get(label, label)
        bucket = out.setdefault(key, [])
        bucket.extend(t for t in terms if t not in bucket)
    return out


ROLE_CATEGORIES = _merge_chips(_FINE_ROLE_CATEGORIES, CHIP_MERGES)

# Titles containing any of these phrases never join the category, even when a term matches
# (same word-boundary matching as the terms). Kept to the one case rewording cannot fix.
ROLE_CATEGORY_EXCLUDE = {
    "Treasury/ALM/Liquidity": ["sales"],  # "Treasury Management Sales Officer" sells treasury products; it belongs under Sales only
    "Portfolio/Asset Mgmt/Wealth": ["operations research"],
    "Quant/Modeling/Pricing": ["valuation controller", "valuation control", "controllers", "valuation advisory"],
}

_EXTRA_SCRIPT = r"""
<script>
function copyText(text) {
  // Clipboard API rejects when the tab lost focus during the fetch; fall back to execCommand.
  return navigator.clipboard.writeText(text).catch(function() {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    if (!ok) throw new Error('copy blocked, click the page and retry');
  });
}
(function() {
  var COMPANY = __COMPANY__;
  var JD_COMPANIES = __JD_COMPANIES__;
  var bar = document.getElementById('filterBar');
  var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr'));
  var HIDE = ['hidden-by-filter', 'hidden-by-company', 'hidden-by-location'];
  var COL_COMPANY = 'company', COL_ROLE = 'role', COL_LOCATION = 'location';
  // Cell lookup by header text, never by position: tables differ (the dedup page adds columns).
  function cellOf(tr, name) {
    var table = tr.closest('table');
    if (!table) return null;
    if (!table._cols) {
      table._cols = {};
      Array.prototype.forEach.call(table.querySelectorAll('thead th'), function(th, i) {
        table._cols[th.textContent.trim().toLowerCase()] = i;
      });
    }
    var i = table._cols[name];
    return i === undefined ? null : (tr.cells[i] || null);
  }
  function textOf(tr, name) { var c = cellOf(tr, name); return c ? c.textContent : ''; }

  var counters = [];
  document.querySelectorAll('h2, summary').forEach(function(el) {
    var table = el.tagName === 'SUMMARY' ? el.parentElement.querySelector('table') : el.nextElementSibling;
    if (!table || table.tagName !== 'TABLE') return;
    var m = el.textContent.match(/^(.*)\((\d+)\)$/);
    if (m) counters.push({el: el, table: table, label: m[1]});
  });
  function recount() {
    counters.forEach(function(c) {
      var n = Array.prototype.filter.call(c.table.querySelectorAll('tbody tr'), function(tr) {
        return !HIDE.some(function(k) { return tr.classList.contains(k); });
      }).length;
      c.el.textContent = c.label + '(' + n + ')';
    });
  }
  ['filterSearchBtn', 'optOnlyBtn', 'springBtn', 'expMin', 'expMax', 'expIncludeNA', 'expReset', 'dateMin', 'dateMax', 'dateIncludeNA'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener(el.tagName === 'BUTTON' ? 'click' : 'change', function() {
      setTimeout(function() { applyCompany(); applyLocation(); }, 0);
    });
  });

  var metroBox = null;
  var resetBtn = document.getElementById('expReset');
  if (resetBtn) resetBtn.addEventListener('click', function() {
    if (metroBox) { metroBox.checked = false; applyLocation(); }
    hideBox.checked = false;
    applyCompany();
  });

  // Hide one company
  var toggles = document.getElementById('fbToggles');
  var label = document.createElement('label');
  label.className = 'chip';
  label.innerHTML = '<input type="checkbox" id="hideCompanyBox"> Hide ' + COMPANY;
  if (toggles && resetBtn) toggles.insertBefore(label, resetBtn);
  else if (bar) bar.appendChild(label);
  var hideBox = label.querySelector('input');
  function applyCompany() {
    rows.forEach(function(tr) {
      var c = textOf(tr, COL_COMPANY).trim();
      tr.classList.toggle('hidden-by-company', hideBox.checked && c === COMPANY);
    });
    recount();
    try { localStorage.setItem('jobScoutHideCompany', hideBox.checked ? '1' : '0'); } catch (e) {}
  }
  try { hideBox.checked = localStorage.getItem('jobScoutHideCompany') === '1'; } catch (e) {}
  hideBox.addEventListener('change', applyCompany);

  // NY metro only: NY/NJ/CT area by location text; upstate NY cities excluded.
  var METRO_RE = /\b(new york|nyc|manhattan|brooklyn|queens|bronx|long island|westchester|white plains|new jersey|nj|jersey city|newark|hoboken|harrison|stamford|greenwich|norwalk|connecticut|ct|ny)\b/;
  var UPSTATE_RE = /\b(buffalo|albany|rochester|syracuse|ithaca|binghamton|utica)\b/;
  var metroLabel = document.createElement('label');
  metroLabel.className = 'chip';
  metroLabel.title = 'NY, NJ and CT locations only';
  metroLabel.innerHTML = '<input type="checkbox" id="metroOnlyBox"> NY metro only';
  label.insertAdjacentElement('afterend', metroLabel);
  var outreachLink = document.createElement('a');
  outreachLink.className = 'chip';
  outreachLink.href = 'https://priyanshukundu.github.io/job_scrape/outreach.html';
  outreachLink.target = '_blank';
  outreachLink.rel = 'noopener';
  outreachLink.style.textDecoration = 'none';
  outreachLink.style.color = 'inherit';
  outreachLink.innerHTML = 'Outreach search links &rarr;';
  metroLabel.insertAdjacentElement('afterend', outreachLink);
  metroBox = metroLabel.querySelector('input');
  function applyLocation() {
    rows.forEach(function(tr) {
      var loc = textOf(tr, COL_LOCATION).toLowerCase();
      var ok = METRO_RE.test(loc) && !UPSTATE_RE.test(loc);
      tr.classList.toggle('hidden-by-location', metroBox.checked && !ok);
    });
    recount();
    try { localStorage.setItem('jobScoutMetroOnly', metroBox.checked ? '1' : '0'); } catch (e) {}
  }
  try { metroBox.checked = localStorage.getItem('jobScoutMetroOnly') === '1'; } catch (e) {}
  metroBox.addEventListener('change', applyLocation);
  applyCompany();
  applyLocation();

  // Copy JD: window.JD_PROXY_URL_OVERRIDE lets the local dedup page point at its own server.
  var JD_URL = window.JD_PROXY_URL_OVERRIDE || __JD_URL__;
  document.addEventListener('click', function(ev) {
    var btn = ev.target.closest ? ev.target.closest('button.copy-act') : null;
    if (!btn) return;
    var tr = btn.closest('tr');
    var td = btn.parentElement;
    var old = td.querySelector('.jd-err');
    if (old) old.remove();
    function fail(msg) {
      btn.disabled = false;
      btn.textContent = 'Copy JD';
      var e = document.createElement('span');
      e.className = 'jd-err';
      e.style.cssText = 'color:#c0392b;font-size:0.75rem;display:block';
      e.textContent = msg;
      td.appendChild(e);
    }
    var roleCell = cellOf(tr, COL_ROLE);
    var roleLink = roleCell ? roleCell.querySelector('a') : null;
    var company = textOf(tr, COL_COMPANY).trim();
    var jdKey = tr.getAttribute('data-jdkey') || '';
    var viaProxy = function() {
      if (!JD_URL) throw new Error('JD proxy not configured');
      return fetch(JD_URL, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          company: company,
          role: (roleLink ? roleLink.textContent : textOf(tr, COL_ROLE)).trim(),
          link: roleLink ? roleLink.href : '',
          location: textOf(tr, COL_LOCATION).trim(),
          jid: tr.getAttribute('data-jid') || '',
          cfg: JD_COMPANIES[company] || null
        })
      }).then(function(resp) {
        if (!resp.ok) throw new Error('server returned ' + resp.status);
        return resp.json();
      }).then(function(data) {
        if (!data.ok) throw new Error(data.stderr || 'request failed');
        return data.text;
      });
    };
    btn.disabled = true;
    btn.textContent = 'Fetching...';
    var fromCache = jdKey ? fetch('jd/' + jdKey + '.txt', {cache: 'no-cache'}).then(function(resp) {
      if (!resp.ok) throw new Error('not cached');
      return resp.text();
    }) : Promise.reject(new Error('no key'));
    fromCache.catch(viaProxy).then(function(text) {
      return copyText(text).then(function() {
        btn.textContent = 'Copied';
        setTimeout(function() { btn.textContent = 'Copy JD'; btn.disabled = false; }, 2000);
      });
    }).catch(function(err) { fail('Failed: ' + err.message); });
  });
})();
</script>
<style>
tr.hidden-by-company, tr.hidden-by-location { display: none; }
.skip-btn { display: inline-block; width: 5.5rem; margin: 0 4px 0 0; padding: 3px 0; font-size: 0.85rem; cursor: pointer; white-space: nowrap; text-align: center; }
td.act { white-space: nowrap; }
</style>
"""


def _jd_company_map():
    """Company -> just the fields a stateless proxy needs to rebuild that ATS's detail URL."""
    out = {}
    link_hosts = {}
    for r in load_json(HOME / "queue.json", []):
        h = urllib.parse.urlparse(r.get("link") or "").hostname
        if h:
            link_hosts.setdefault(r.get("company"), set()).add(h)
    for c in load_json(HOME / "config.json", {}).get("companies", []):
        ats = c.get("ats")
        if ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            entry = dict(ats=ats, slug=c["slug"])
            if c.get("region"):
                entry["region"] = c["region"]
        elif ats == "workday":
            sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
            entry = dict(ats=ats, host=c["host"], tenant=c["tenant"], sites=sites)
        elif ats == "oracle":
            entry = dict(ats=ats, host=c["host"])
        else:
            hosts = set(c.get("hosts") or [])
            for k in ("host", "board"):
                if c.get(k):
                    hosts.add(urllib.parse.urlparse(c[k] if "//" in c[k] else "//" + c[k]).hostname)
            hosts |= link_hosts.get(c["name"], set())
            if not hosts:
                continue
            entry = dict(ats="page", hosts=sorted(h for h in hosts if h))  # proxy falls back to reading the posting page
        out[c["name"]] = entry
    return out


def _extra_script():
    return (_EXTRA_SCRIPT
            .replace("__COMPANY__", json.dumps(HIDE_COMPANY))
            .replace("__JD_COMPANIES__", json.dumps(_jd_company_map(), separators=(",", ":")))
            .replace("__JD_URL__", json.dumps(JD_PROXY_URL)))


def write_queue_html(q, path, updated_at=None):
    """Static page, no server: q is already sorted newest-posted-first by cmd_queue. Each row is a
    plain <a> to the live posting so double-clicking the file and clicking a link is the whole workflow.
    "new" postings get their own table up top; every other open state (shortlisted, applied, ...)
    collapses into a <details> section; closed/removed entries (any state) go in one final section so
    a growing history doesn't bury what's actionable today."""
    def row_html(e):
        posted = e.get("posted") or ""
        closed = bool(e.get("closed_on"))
        # No relative label ("today", "seen MM-DD") is baked in here: this is a static page with
        # no server, generated once per scheduled run, so any relative-to-now text written at
        # generation time goes stale the moment the viewer's clock moves past that instant (e.g.
        # a posting stays labeled "today" forever if viewed the next day off a page that hasn't
        # regenerated). The raw ISO date (or the id-only fallbacks below) goes in data-posted /
        # data-first-seen instead, and the script at the bottom recomputes the relative label and
        # the "posted today" highlight against the viewer's own current date on every page load.
        kw = ", ".join(e["kw_hits"]) if e["kw_hits"] else "—"
        spons_evidence = html.escape(e.get("sponsorship_evidence") or "no blocking language found")
        exp_years = e.get("experience")
        exp_label = f"{exp_years}+ years" if isinstance(exp_years, int) else "N/A"
        exp_evidence = html.escape(e.get("experience_evidence") or "")
        salary = e.get("salary") or "Unclear"
        salary_evidence = html.escape(e.get("salary_evidence") or "")
        spons_label = {"Unclear": "N/A", "OPT Only": "No H1B"}.get(e["sponsorship_status"], e["sponsorship_status"])
        salary_label = salary if salary != "Unclear" else "N/A"
        role = html.escape(e["role"])
        if e.get("repost_count"):
            role += f' <span class="repost" title="repost of {html.escape(e.get("repost_of") or "?")}">repost x{e["repost_count"]}</span>'
        if e.get("sponsorship_status") == "OPT Only":
            _ev = html.escape(e.get("sponsorship_evidence") or "")
            role = f'<span class="optbadge" title="{_ev}">No H1B</span>' + role
        role_cell = (f'<a href="{html.escape(e["link"])}" target="_blank" rel="noopener">{role}</a>'
                     if not closed else role)
        row_title = ""
        if closed:
            row_title = (f' title="closed {html.escape(e["closed_on"])} ({html.escape(e.get("closed_reason") or "")}); '
                         f'last live {html.escape(e.get("last_seen_live") or "unknown")}"')
        state_cell = f'<td>{html.escape(e["state"])}</td>' if closed else ""
        exp_data = f' data-exp="{exp_years}"' if isinstance(exp_years, int) else ' data-exp="-1"'
        first_seen = e.get("first_seen") or ""
        opt_data = ' data-opt="1"' if e.get("sponsorship_status") == "OPT Only" else ""
        if _SPRING_2027_RE.search(e.get("role") or ""):
            opt_data += ' data-spring="1"'
        row_class = e["state"] + (" closed" if closed else "")
        if e.get("posted_source") == "approx":
            posted_title = "approximate - derived from Workday's relative posting-age text, not an exact timestamp"
        elif posted and e.get("posted_source") == "exact":
            posted_title = "exact date from the source"
        else:
            posted_title = "no posting date available from the source - this is when job-scout first discovered it, not when it was actually posted"
        return (
            f'<tr class="{row_class}"{row_title}{exp_data} data-jid="{html.escape(e["id"].split(":", 1)[-1])}"'
            f' data-jdkey="{hashlib.sha1(e["id"].encode("utf-8")).hexdigest()[:20]}"'
            f' data-posted="{html.escape(posted)}" data-first-seen="{html.escape(first_seen)}"'
            f' data-posted-source="{html.escape(e.get("posted_source") or "")}"{opt_data}>'
            f'<td class="posted" title="{html.escape(posted_title)}">{html.escape(posted_label(e))}</td>'
            f'<td>{html.escape(e["company"])}</td>'
            f'<td>{role_cell}</td>'
            f'{"" if closed else ACTIONS_TD}'
            f'<td>{html.escape(e["location"])}</td>'
            f'<td title="{spons_evidence}">{html.escape(spons_label)}</td>'
            f'<td title="{exp_evidence}">{html.escape(exp_label)}</td>'
            f'<td title="{salary_evidence}" class="salary">{html.escape(salary_label)}</td>'
            f'<td class="kw">{html.escape(kw)}</td>'
            f'{state_cell}'
            f'</tr>'
        )

    head = ("<tr><th>Posted</th><th>Company</th><th>Role</th><th>Actions</th><th>Location</th>"
            "<th>Sponsorship</th><th>Experience</th><th>Salary</th><th>Keywords</th></tr>")
    closed_head = head.replace("<th>Actions</th>", "").replace("</tr>", "<th>State</th></tr>")

    open_q = [e for e in q if not e.get("closed_on")]
    closed_q = [e for e in q if e.get("closed_on")]
    new_rows = [e for e in open_q if e["state"] in ("new", "opt_only")]  # OPT-only roles sit in the open list, filterable via the OPT only button
    by_state = {}
    for e in open_q:
        if e["state"] not in ("new", "opt_only"):
            by_state.setdefault(e["state"], []).append(e)
    ordered_states = [s for s in _SECONDARY_STATE_ORDER if s in by_state] + \
                      [s for s in by_state if s not in _SECONDARY_STATE_ORDER]

    sections = "".join(
        f'<details><summary>{html.escape(_STATE_LABELS.get(state, state))} ({len(by_state[state])})</summary>'
        f'<table><thead>{head}</thead><tbody>{"".join(row_html(e) for e in by_state[state])}</tbody></table>'
        f'</details>'
        for state in ordered_states
    )
    if closed_q:
        sections += (
            f'<details><summary>closed / removed ({len(closed_q)})</summary>'
            f'<table><thead>{closed_head}</thead><tbody>{"".join(row_html(e) for e in closed_q)}</tbody></table>'
            f'</details>'
        )

    updated_label = updated_at.strftime("%Y-%m-%d %H:%M %Z") if updated_at else "unknown"
    updated_iso = updated_at.isoformat(timespec="seconds") if updated_at else ""
    # Employers the scout cannot poll yet, from data/unsupported.json; empty file means no dropdown
    uns = [c for c in load_json(HOME / "unsupported.json", {}).get("companies", []) if c.get("name")]
    unsupported_html = ""
    if uns:
        items = "".join(f"<li>{html.escape(c['name'])}<span>{html.escape(c.get('reason', ''))}</span></li>"
                        for c in sorted(uns, key=lambda c: c["name"].lower()))
        unsupported_html = (f'<details id="unsupported"><summary title="Employers this scout cannot read yet">Not covered ({len(uns)})</summary>'
                            f'<div class="uns-panel"><p>Postings from these employers will not appear here. Check their sites directly.</p>'
                            f'<ul>{items}</ul></div></details>')

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Jobs Tracker</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; color: #111; }}
h1 {{ font-size: 1.2rem; }}
.meta {{ color: #666; font-size: 0.85rem; margin-top: -0.5rem; margin-bottom: 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9rem; }}
th {{ position: sticky; top: 0; background: #fafafa; }}
tr.today {{ background: #eaffea; font-weight: 600; }}
tr.closed {{ color: #999; }}
tr.closed a, tr.closed {{ text-decoration: line-through; }}
td.posted {{ white-space: nowrap; }}
.approx-marker {{ font-weight: 700; color: #b45309; }}
td.kw {{ color: #555; font-size: 0.85rem; }}
td.salary {{ white-space: nowrap; }}
span.repost {{ color: #b45309; font-weight: 600; font-size: 0.8rem; text-decoration: none; }}
details {{ margin-bottom: 0.5rem; }}
summary {{ cursor: pointer; font-weight: 600; padding: 4px 0; }}
#filterBar {{ position: sticky; top: 0; background: #fafafa; padding: 0.6rem 0; margin-bottom: 0.5rem; z-index: 1; border-bottom: 1px solid #e3e3e3; display: flex; flex-direction: column; gap: 8px; }}
#filterBox {{ width: 100%; max-width: 520px; padding: 8px 10px; font-size: 1rem; box-sizing: border-box; }}
#expReset {{ padding: 5px 14px; font-size: 0.9rem; cursor: pointer; border: 1px solid #999; border-radius: 999px; background: #fff; }}
#optOnlyLbl {{ color: #8a5a00; border-color: #cc9a2e; }}
#optOnlyLbl:has(input:checked) {{ background: #f6e3b4; border-color: #b8801a; }}
#springLbl {{ color: #1f5f8b; border-color: #5b9bc8; }}
#springLbl:has(input:checked) {{ background: #cfe4f3; border-color: #2f77ad; }}
.optbadge {{ font-size: 0.7rem; color: #fff; background: #c0392b; border-radius: 4px; padding: 1px 5px; margin-right: 5px; font-weight: 600; letter-spacing: 0.02em; white-space: nowrap; }}
#expReset {{ margin-left: auto; color: #555; }}
.fb-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }}
.fb-row .lbl {{ font-size: 0.72rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: #666; min-width: 7.5rem; }}
.fb-row .lbl.sep {{ margin-left: 18px; min-width: 0; }}
.fb-row .to {{ color: #888; font-size: 0.85rem; }}
.fb-row select {{ padding: 4px 6px; font-size: 0.9rem; }}
label.chip {{ display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border: 1px solid #ccc; border-radius: 999px; background: #fff; font-size: 0.85rem; cursor: pointer; white-space: nowrap; }}
label.chip:has(input:checked) {{ background: #e6f0fb; border-color: #6aa5e0; }}
#filterSearchBtn {{ padding: 8px 14px; font-size: 1rem; cursor: pointer; }}
#filterCount {{ color: #666; font-size: 0.85rem; margin-left: 8px; }}
tr.hidden-by-filter {{ display: none; }}
.exp-filter {{ margin-top: 6px; font-size: 0.85rem; color: #333; }}
.exp-filter select {{ padding: 3px 6px; font-size: 0.85rem; margin: 0 4px; }}
#unsupported {{ margin: 0 0 0 auto; position: relative; order: 98; }}
#unsupported summary {{ list-style: none; padding: 5px 14px; font-size: 0.9rem; font-weight: 400; cursor: pointer; border: 1px solid #999; border-radius: 999px; background: #fff; color: #555; }}
#unsupported summary::-webkit-details-marker {{ display: none; }}
#unsupported[open] summary {{ background: #f1f1f1; }}
#unsupported .uns-panel {{ position: absolute; right: 0; top: 34px; width: 340px; max-height: 340px; overflow-y: auto; background: #fff; border: 1px solid #ccc; border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.15); padding: 8px 12px; z-index: 5; font-size: 0.85rem; }}
#unsupported .uns-panel p {{ margin: 0 0 6px; color: #666; font-size: 0.78rem; }}
#unsupported .uns-panel ul {{ margin: 0; padding: 0; list-style: none; }}
#unsupported .uns-panel li {{ display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; border-top: 1px solid #eee; }}
#unsupported .uns-panel li span {{ color: #777; text-align: right; }}
#expReset {{ order: 99; margin-left: 8px; }}
</style></head>
<body>
<h1>Job Scout Queue - {len(open_q)} open, {len(closed_q)} closed</h1>
<p class="meta">Data last updated {updated_label} &middot; <span class="approx-marker">~</span> before a Posted date means approximate (day-count bucket, not an exact timestamp) - hover any date for details</p>
<div id="stale-banner" hidden style="background:#b00020;color:#fff;padding:10px 14px;border-radius:6px;margin:8px 0;font-weight:600"></div>
<script>
(function() {{
  var updated = new Date("{updated_iso}");
  var hours = (Date.now() - updated.getTime()) / 3600000;
  if (!isNaN(hours) && hours > {STALE_ALERT_HOURS}) {{
    var b = document.getElementById("stale-banner");
    b.textContent = "Scout has not updated for " + Math.floor(hours) + " hours (last run " + "{updated_label}" + "). Press Ctrl+F5 first (this may be a cached copy); if it persists the workflow may be stuck, check GitHub Actions.";
    b.hidden = false;
  }}
}})();
</script>
<div id="filterBar">
<div class="fb-row">
<input type="search" id="filterBox" placeholder="Filter: comma = OR, space = AND; &quot;ey&quot; = whole word; company:ey, title:risk, loc:&quot;new york&quot; = that column only" autocomplete="off">
<button type="button" id="filterSearchBtn">Search</button>
<span id="filterCount"></span>
</div>
<div class="fb-row" id="fbToggles">
<span class="lbl">Show</span>
<label class="chip" id="optOnlyLbl" title="Hide roles that refuse employer sponsorship but do not exclude OPT or STEM OPT"><input type="checkbox" id="optOnlyBtn"> Hide No H1B jobs</label>
<label class="chip" id="springLbl" title="Hide Spring 2027 internships and programs"><input type="checkbox" id="springBtn"> Hide Spring 2027</label>
{unsupported_html}
<button type="button" id="expReset">Reset all filters</button>
</div>
<div class="fb-row">
<span class="lbl">Experience (years)</span>
<select id="expMin">
{''.join(f'<option value="{n}"{" selected" if n == 0 else ""}>{n}</option>' for n in range(16))}
</select>
<span class="to">to</span>
<select id="expMax">
{''.join(f'<option value="{n}">{n}</option>' for n in range(16))}
<option value="inf" selected>15+</option>
</select>
<label class="chip" title="Keep roles with no years-of-experience found"><input type="checkbox" id="expIncludeNA" checked> include N/A</label>
<span class="lbl sep">Posted (days ago)</span>
<select id="dateMin">
{''.join(f'<option value="{n}"{" selected" if n == 0 else ""}>{n}</option>' for n in DATE_FILTER_DAYS)}
</select>
<span class="to">to</span>
<select id="dateMax">
{''.join(f'<option value="{n}">{n}</option>' for n in DATE_FILTER_DAYS)}
<option value="inf" selected>any</option>
</select>
<label class="chip" title="Keep roles with no posting date"><input type="checkbox" id="dateIncludeNA" checked> include undated</label>
</div>
</div>
<h2>New ({len(new_rows)})</h2>
<table id="q">
<thead>{head}</thead>
<tbody>
{''.join(row_html(e) for e in new_rows)}
</tbody>
</table>
{sections}
<script>
(function() {{
  // Client-side filter, no server: comma-separated groups are OR'd, terms within a group
  // are AND'd (e.g. "python, sql bloomberg" = python OR (sql AND bloomberg)). Lets one shared
  // page/URL serve different people with different interests - each viewer just types their own
  // terms; nothing is sent anywhere and nothing is saved except this browser's own last search.
  // The text box only re-filters on Search-click/Enter, not per keystroke - with several thousand
  // rows, re-scanning tr.textContent on every character typed was visibly laggy. The exp/N-A
  // controls stay live since a dropdown change is a single cheap event, not one per keystroke.
  var box = document.getElementById('filterBox');
  var searchBtn = document.getElementById('filterSearchBtn');
  var countEl = document.getElementById('filterCount');
  var expMin = document.getElementById('expMin');
  var expMax = document.getElementById('expMax');
  var expIncludeNA = document.getElementById('expIncludeNA');
  var expReset = document.getElementById('expReset');
  var dateMin = document.getElementById('dateMin');
  var dateMax = document.getElementById('dateMax');
  var dateIncludeNA = document.getElementById('dateIncludeNA');
  var optBtn = document.getElementById('optOnlyBtn');  // checkbox: hide OPT only rows
  var optOnly = false;
  var springBtn = document.getElementById('springBtn');  // checkbox: hide Spring 2027 rows
  var hideSpring = false;
  var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr'));
  var forcedOpen = [];
  function setOpt(on) {{ optOnly = on; optBtn.checked = on; }}
  function setSpring(on) {{ hideSpring = on; springBtn.checked = on; }}

  function ageDays(tr) {{
    var iso = tr.getAttribute('data-posted') || tr.getAttribute('data-first-seen');
    if (!iso) return null;
    var now = new Date(), t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((t0 - new Date(iso + 'T00:00:00')) / 86400000);
  }}
  function dateActive() {{ return dateMin.value !== '0' || (dateMax.value !== 'inf' && !window.__dateIgnoreMax); }}
  function dateOk(tr) {{
    if (!dateActive()) return true;
    var a = ageDays(tr);
    if (a === null) return dateIncludeNA.checked;
    var lo = parseInt(dateMin.value, 10);
    var hi = (dateMax.value === 'inf' || window.__dateIgnoreMax) ? Infinity : parseInt(dateMax.value, 10);
    return a >= lo && a <= hi;
  }}

  // Term syntax: plain word = substring anywhere in the row (3 chars or fewer = whole word, so ey no longer hits money); "ey" (quotes) = whole word or phrase anywhere;
  // company:ey / title:risk / loc:"new york" = whole word in that column only; a trailing * allows a prefix (title:treas*).
  var FIELD_ALIAS = {{company: 'company', title: 'role', role: 'role', loc: 'location', location: 'location'}};
  function parseTerms(g) {{
    var out = [], re = /(?:([a-z]+):)?(?:"([^"]*)"|(\\S+))/g, m;
    while ((m = re.exec(g)) !== null) {{
      var field = m[1] && FIELD_ALIAS[m[1]] ? FIELD_ALIAS[m[1]] : null;
      var body = m[2] !== undefined ? m[2] : m[3];
      var word = (m[1] && !field ? m[1] + ':' + body : body).trim();
      var prefix = word.charAt(word.length - 1) === '*';
      if (prefix) word = word.slice(0, -1);
      if (!word) continue;
      var rx = null;
      if (field || m[2] !== undefined || word.length <= 3) {{
        rx = new RegExp('(^|[^a-z0-9])' + word.replace(/[.*+?^$(){{}}|[\\]\\\\]/g, '\\\\$&') + (prefix ? '' : '([^a-z0-9]|$)'));
      }}
      out.push({{word: word, field: field, rx: rx}});
    }}
    return out;
  }}
  var COLS = null;
  function colIndex(name) {{
    if (!COLS) {{
      COLS = {{}};
      Array.prototype.forEach.call(document.querySelectorAll('table thead th'), function(h, i) {{ COLS[h.textContent.trim().toLowerCase()] = i; }});
    }}
    return COLS[name];
  }}
  function termOk(tr, text, t) {{
    if (t.field) {{
      var i = colIndex(t.field), c = i === undefined ? null : tr.cells[i];
      return !!c && t.rx.test(c.textContent.toLowerCase());
    }}
    if (!t.rx) return text.indexOf(t.word) !== -1;
    return Array.prototype.some.call(tr.cells, function(c) {{ return t.rx.test(c.textContent.toLowerCase()); }});
  }}

  function apply() {{
    var raw = box.value.trim().toLowerCase();
    var lo = parseInt(expMin.value, 10);
    var hi = expMax.value === 'inf' ? Infinity : parseInt(expMax.value, 10);
    forcedOpen.forEach(function(d) {{ d.removeAttribute('open'); }});
    forcedOpen = [];
    var groups = raw ? raw.split(',').map(function(g) {{ return parseTerms(g); }})
                          .filter(function(g) {{ return g.length; }}) : null;
    var expActive = !(lo === 0 && hi === Infinity);
    var shown = 0;
    rows.forEach(function(tr) {{
      var textMatch = true;
      if (groups) {{
        var text = tr.textContent.toLowerCase();
        textMatch = groups.some(function(terms) {{ return terms.every(function(t) {{ return termOk(tr, text, t); }}); }});
      }}
      var expMatch = true;
      if (expActive) {{
        var rank = parseInt(tr.getAttribute('data-exp'), 10);
        // -1 means "no years-of-experience language found" - never silently drop those from a
        // numeric range filter (they didn't fail the check, they just have nothing to check).
        expMatch = rank === -1 ? expIncludeNA.checked : (rank >= lo && rank <= hi);
      }}
      var match = textMatch && expMatch && dateOk(tr) && (!optOnly || tr.getAttribute('data-opt') !== '1') && (!hideSpring || tr.getAttribute('data-spring') !== '1');
      tr.classList.toggle('hidden-by-filter', !match);
      if (match) {{
        shown++;
        var details = tr.closest('details');
        if (details && !details.open) {{ details.open = true; forcedOpen.push(details); }}
      }}
    }});
    countEl.textContent = (raw || expActive || dateActive() || optOnly || hideSpring) ? ('showing ' + shown + ' of ' + rows.length) : '';
    try {{
      localStorage.setItem('jobScoutFilter', box.value);
      localStorage.setItem('jobScoutExpMin', expMin.value);
      localStorage.setItem('jobScoutExpMax', expMax.value);
      localStorage.setItem('jobScoutExpIncludeNA', expIncludeNA.checked ? '1' : '0');
      localStorage.setItem('jobScoutDateMin', dateMin.value);
      localStorage.setItem('jobScoutDateMax', dateMax.value);
      localStorage.setItem('jobScoutDateNA', dateIncludeNA.checked ? '1' : '0');
      localStorage.setItem('jobScoutHideOpt', optOnly ? '1' : '0');
      localStorage.setItem('jobScoutHideSpring', hideSpring ? '1' : '0');
    }} catch (e) {{}}
  }}

  expReset.addEventListener('click', function() {{
    box.value = '';
    expMin.value = '0';
    expMax.value = 'inf';
    expIncludeNA.checked = true;
    dateMin.value = '0';
    dateMax.value = 'inf';
    dateIncludeNA.checked = true;
    setOpt(false);
    setSpring(false);
    apply();
  }});

  try {{
    var saved = localStorage.getItem('jobScoutFilter');
    if (saved) box.value = saved;
    var savedMin = localStorage.getItem('jobScoutExpMin');
    var savedMax = localStorage.getItem('jobScoutExpMax');
    var savedNA = localStorage.getItem('jobScoutExpIncludeNA');
    if (savedMin !== null) expMin.value = savedMin;
    if (savedMax !== null) expMax.value = savedMax;
    if (savedNA !== null) expIncludeNA.checked = savedNA === '1';
    var sdMin = localStorage.getItem('jobScoutDateMin');
    var sdMax = localStorage.getItem('jobScoutDateMax');
    var sdNA = localStorage.getItem('jobScoutDateNA');
    if (sdMin !== null) dateMin.value = sdMin;
    if (sdMax !== null) dateMax.value = sdMax;
    if (sdNA !== null) dateIncludeNA.checked = sdNA === '1';
    setOpt(localStorage.getItem('jobScoutHideOpt') === '1');
    setSpring(localStorage.getItem('jobScoutHideSpring') === '1');
  }} catch (e) {{}}
  optBtn.addEventListener('change', function() {{ setOpt(optBtn.checked); apply(); }});
  springBtn.addEventListener('change', function() {{ setSpring(springBtn.checked); apply(); }});
  searchBtn.addEventListener('click', apply);
  box.addEventListener('keydown', function(e) {{ if (e.key === 'Enter') {{ e.preventDefault(); apply(); }} }});
  expMin.addEventListener('change', apply);
  expMax.addEventListener('change', apply);
  expIncludeNA.addEventListener('change', apply);
  dateMin.addEventListener('change', apply);
  dateMax.addEventListener('change', apply);
  dateIncludeNA.addEventListener('change', apply);
  apply();
}})();
(function() {{
  // Recomputes "today"/"N days ago"/"seen MM-DD" against the viewer's own current date, every
  // page load - the server only ever writes the raw ISO date into data-posted/data-first-seen,
  // never a relative label, so this never goes stale even if the page itself wasn't regenerated
  // since yesterday.
  function daysAgo(iso) {{
    var d = new Date(iso + 'T00:00:00');
    var now = new Date();
    var todayLocal = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((todayLocal - d) / 86400000);
  }}
  document.querySelectorAll('tr[data-posted]').forEach(function(tr) {{
    var posted = tr.getAttribute('data-posted');
    var cell = tr.querySelector('td.posted');
    // "~" marks a date derived from Workday's relative day-count text rather than an exact
    // timestamp (see posted_label()/posted_source server-side) - carried through here so the
    // marker survives the dynamic relabel instead of only appearing on first render.
    var prefix = tr.getAttribute('data-posted-source') === 'approx' ? '~' : '';
    var label;
    if (posted) {{
      var n = daysAgo(posted);
      label = prefix + (n <= 0 ? 'today' : n === 1 ? 'yesterday' : n + 'd ago');
      tr.classList.toggle('today', n <= 0);
    }} else {{
      var firstSeen = tr.getAttribute('data-first-seen');
      label = firstSeen ? 'seen ' + firstSeen.slice(5) : 'unknown';
      tr.classList.remove('today');
    }}
    if (cell) cell.textContent = label;
  }});
  // Row order is dynamic too, not just the label - re-sort each table body by the same date every
  // load (today, then yesterday, then further back; undated rows last) so the order self-corrects
  // exactly like the label does, instead of staying frozen at whatever order the last regeneration
  // computed it in.
  document.querySelectorAll('table tbody').forEach(function(tbody) {{
    var trs = Array.prototype.slice.call(tbody.children);
    trs.sort(function(a, b) {{
      var ka = a.getAttribute('data-posted') || a.getAttribute('data-first-seen') || '';
      var kb = b.getAttribute('data-posted') || b.getAttribute('data-first-seen') || '';
      if (ka === kb) return 0;
      if (!ka) return 1;
      if (!kb) return -1;
      return ka < kb ? 1 : -1;
    }});
    trs.forEach(function(tr) {{ tbody.appendChild(tr); }});
  }});
}})();
</script>
{_extra_script()}
<button type="button" id="blockedJump" title="Jump to the Blocked section (citizenship, clearance or STEM OPT excluded)" style="display:none;position:fixed;right:0;top:45%;z-index:50;background:#7b241c;color:#fff;border:0;border-radius:6px 0 0 6px;padding:8px 6px;font-size:0.75rem;font-weight:600;cursor:pointer;writing-mode:vertical-rl;letter-spacing:0.03em">Blocked &#9662;</button>
<script>
(function() {{
  var btn = document.getElementById('blockedJump');
  var sec = Array.prototype.filter.call(document.querySelectorAll('details'), function(d) {{
    var s = d.querySelector('summary');
    return s && /^Blocked/.test(s.textContent.trim());
  }})[0];
  if (!sec) return;  // no Blocked section on the page, so the button stays hidden
  btn.style.display = 'block';
  btn.addEventListener('click', function() {{
    sec.setAttribute('open', '');
    sec.scrollIntoView({{block: 'start', behavior: 'smooth'}});
  }});
}})();
</script>
</body></html>"""
    Path(path).write_text(page, encoding="utf-8")


def cmd_mark(args):
    q = load_json(HOME / "queue.json", [])
    hit = [e for e in q if e["id"] == args.id]
    if not hit:
        sys.exit(f"no queue entry {args.id}")
    e = hit[0]
    if args.state == "closed":  # manual: for a dead link the scout's own liveness check missed
        e["closed_on"], e["closed_reason"] = date.today().isoformat(), "manual"
    elif args.state == "open":
        e["closed_on"], e["closed_reason"], e["miss_count"] = None, None, 0
    elif args.state in QUEUE_STATES:
        e["state"] = args.state
    else:
        sys.exit(f"state must be one of {QUEUE_STATES + ('closed', 'open')}")
    atomic_json.write(str(HOME / "queue.json"), q)
    print(f"{args.id} -> {args.state}")


def run(args):
    cfg = load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}. Run: python job_scout.py --init")
    _load_title_terms(cfg)
    filters = cfg.get("filters", {})
    keywords = cfg.get("keywords", [])
    state = load_json(HOME / "state.json", {})
    queue = load_json(HOME / "queue.json", [])
    queued_ids = {e["id"] for e in queue}
    run_id = "scout_" + date.today().isoformat()
    counts = dict(companies=0, errors=0, matched=0, blocked=0, queued=0, alerted=0,
                  closed=0, reopened=0, reposts=0, pruned=0)
    filters_sig = _filters_signature(filters)
    if not args.dry_run and not args.check and not getattr(args, "recall", False)             and (HOME / "filters_sig.json").exists()             and load_json(HOME / "filters_sig.json", {}).get("sig") != filters_sig:
        args.recall = True  # filters changed since the last clean run: roles seen under the old filters may match now
        args.auto_recall = True
        print("filters changed since the last clean run: recalling previously seen roles that match now")

    if args.from_fetched:
        fetched = load_fetched(args.from_fetched, cfg["companies"])
        if not args.dry_run:
            update_fetch_times(args.from_fetched)
            update_board_freshness(fetched)
    else:
        fetched, _ = fetch_all(cfg["companies"])

    # One shared pool for every description fetch across every company, not one pool per company -
    # several companies finishing their listing fetch around the same time and each spinning up
    # their own pool caused a brief concurrency spike that tripped Workday's rate limiting (429s).
    describe_pool = ThreadPoolExecutor(max_workers=8)
    floods = []  # (company, n) for companies held back by the flood guard
    tombstones = _load_tombstones() if getattr(args, "recall", False) else set()
    unverified = {c["name"] for c in cfg["companies"]}  # companies not cleanly and completely fetched this run: never prune their entries
    try:
        def prepare(c):
            """Filter, rescue multi-site postings and start the description fetches, so company N+1's slow
            rescue and company N's descriptions overlap instead of queueing behind each other."""
            name = c["name"]
            jobs, complete, err = fetched[name]
            if err is not None:
                return None
            jobs = list({j["id"]: j for j in jobs}.values())  # a fetcher that repeats an id must not double-queue it
            hits = [j for j in jobs if matches(j, filters)]
            if args.check:
                return jobs, hits, None, False, []
            hits += _rescue_multi_site(c, jobs, hits, filters)
            seen = set(state.get(name, []))
            first_run = name not in state
            fresh = [j for j in (hits if first_run else [j for j in hits if j["id"] not in seen])
                     if f"{name}:{j['id']}" not in queued_ids]
            return jobs, hits, {describe_pool.submit(describe, c, j): j for j in fresh}, first_run, fresh

        prep_pool = ThreadPoolExecutor(max_workers=4)  # separate from describe_pool: prepare() waits on nothing in it
        preps = {c["name"]: prep_pool.submit(prepare, c) for c in cfg["companies"]}
        for c in cfg["companies"]:
            name = c["name"]
            jobs, complete, err = fetched[name]
            if err is None and complete and jobs:
                unverified.discard(name)
            if err is not None:
                counts["errors"] += 1
                print(f"! {name} ({c['ats']}): {err}", file=sys.stderr)
                continue
            counts["companies"] += 1
            prepared = preps[name].result()
            jobs, hits = prepared[0], prepared[1]
            if args.check:
                warn = "  <- 0 jobs, check slug" if not jobs else ""
                trunc = "  [truncated - raise max_per_term or narrow search terms]" if not complete else ""
                print(f"ok {name}: {len(jobs)} jobs, {len(hits)} match{warn}{trunc}")
                continue

            _jobs, _hits, futures, first_run, fresh = prepared
            seen = set(state.get(name, []))
            descriptions = {}
            if fresh:
                for fut in as_completed(futures):
                    j = futures[fut]
                    try:
                        text, posted, posted_source = fut.result()
                        descriptions[j["id"]] = text
                        if posted:  # refines/replaces the bulk listing's date with the detail page's exact one
                            j["posted"] = posted
                            j["posted_source"] = posted_source
                    except Exception as e:
                        print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
                        descriptions[j["id"]] = ""
                        j["desc_failed"] = True
            flood = (not first_run and len(fresh) > max(FLOOD_MIN, FLOOD_FRACTION * len(hits)))
            if flood:
                print(f"! {name}: {len(fresh)}/{len(hits)} matches are new at once (id or search-term change?), "
                      f"queueing silently with one summary alert", file=sys.stderr)
                floods.append((name, len(fresh)))
            _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                              keywords, cfg, args, counts, silent=flood)
            if getattr(args, "recall", False) and not first_run:
                _recall_company(c, name, hits, seen, queued_ids, tombstones, describe_pool, queue, keywords, cfg, args, counts)
            if not args.dry_run:
                _update_liveness(name, jobs, complete, queue, counts, c)
    finally:
        prep_pool.shutdown(cancel_futures=True)
        describe_pool.shutdown()

    if args.check:
        return
    _retry_pending_alerts(queue, cfg.get("notify", {}), counts, args.dry_run)
    _send_summaries(cfg.get("notify", {}), floods, counts, args.dry_run)
    if not args.dry_run:
        _apply_refreshed_dates()
        _reverify_recent_posted(queue, cfg)  # a real network pass - skip on --dry-run like everything else
        _retry_enrichment(queue, cfg, keywords)
        _fill_dates_from_id(queue, cfg)
    _classify_ghosts(queue)  # queue-wide, so it must run after every company's entries exist
    if not args.dry_run:
        _prune_stale(queue, counts, unverified, configured={c["name"] for c in cfg["companies"]})
        _write_tombstones(counts.get("pruned_ids", []), "pruned")
        _drop_location_filtered(queue, cfg.get("filters", {}), counts)
        HOME.mkdir(parents=True, exist_ok=True)
        # queue first: a crash between the writes must never leave a job marked seen without its queue row
        atomic_json.write(str(HOME / "queue.json"), queue)
        atomic_json.write(str(HOME / "state.json"), state)
        # sig only after a clean, uncapped recall; otherwise the next run retries the leftovers
        if getattr(args, "recall", False) and counts["errors"] == 0 and counts.get("recalled", 0) < RECALL_MAX:
            atomic_json.write(str(HOME / "filters_sig.json"), dict(sig=filters_sig, on=date.today().isoformat()))
    print(f"done: {counts['companies']} companies ({counts['errors']} errors), {counts['matched']} new matches, "
          f"{counts['blocked']} auto-blocked on sponsorship, {counts['queued']} queued, {counts['alerted']} alerts, "
          f"{counts['closed']} closed, {counts['reopened']} reopened, {counts['reposts']} reposts, "
          f"{counts['pruned']} pruned (posted > {PRUNE_AFTER_DAYS}d ago).")


def _drop_location_filtered(queue, filters, counts):
    """Removes still-new queue rows whose location a newer locations_exclude term (or location regex) rejects, so
    the hosted page matches the config without a hand-edit of queue.json. Location only (never the title lists),
    not tombstoned: if a term is later loosened the row simply comes back on the next fetch."""
    loc_only = {"title_include": [], "title_exclude": [], "locations_include": filters.get("locations_include", []),
                "locations_exclude": filters.get("locations_exclude", [])}
    keep = [e for e in queue if e.get("state") != "new" or matches({"title": e.get("role", "x"), "location": e.get("location", ""), "link": e.get("link", "")}, loc_only)]
    counts["location_dropped"] = len(queue) - len(keep)
    queue[:] = keep


def _find_repost_source(queue, name, fp, live_ids, eid, claimed):
    """A prior queue entry is this job's repost source if: same company, same role fingerprint,
    its own id is no longer in the live listing (so it's not just a second concurrent opening for
    the same role), not already claimed by another fresh match this same run (two different new
    postings can share a fingerprint - the second one to process must not steal the first one's
    source), and it went quiet recently enough that the match isn't just coincidence.

    The recency check is keyed on when the prior posting was last known live (closed_on, falling
    back to last_seen_live), not on when it was first_seen - a role that stayed posted for months
    before finally closing has an old first_seen even if it just went quiet last week, and gating
    on first_seen would wrongly treat that as "too stale to be a repost". This matters most for
    exactly the long-lived/evergreen postings a repost check should be catching."""
    cutoff = (date.today() - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    candidates = [e for e in queue if e["company"] == name and e["id"] != eid
                  and role_fingerprint(e["role"], e["location"]) == fp
                  and e["id"].split(":", 1)[1] not in live_ids
                  and e["id"] not in claimed
                  and (e.get("closed_on") or e.get("last_seen_live") or e.get("first_seen") or "") >= cutoff]
    return max(candidates, key=lambda e: e.get("first_seen", "")) if candidates else None


def _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                      keywords, cfg, args, counts, silent=False):
    live_ids = {jj["id"] for jj in jobs}
    today = date.today().isoformat()
    claimed = set()  # prior entries already matched to a fresh posting earlier in this same loop -
                      # without this, two different fresh postings that share a fingerprint could
                      # both claim the same prior as their repost source
    for j in fresh:
        counts["matched"] += 1
        eid = f"{name}:{j['id']}"
        text = descriptions.get(j["id"], "")
        spons, evidence = classify_sponsorship(text)
        experience, experience_evidence = classify_experience(j["title"], text)
        salary, salary_evidence = classify_salary(text)
        fp = role_fingerprint(j["title"], j["location"])
        # a capped/paginated listing can't prove a prior id is gone - it may just be outside the
        # window - so only trust absence as repost evidence when this run's listing was complete
        prior = None if (first_run or not complete) else _find_repost_source(queue, name, fp, live_ids, eid, claimed)

        entry_state = {"Blocked": "auto_blocked", "OPT Only": "opt_only"}.get(spons, "new")
        repost_count, repost_of, reposted_on = 0, None, None
        lineage_root, lineage_first_seen = eid, today
        if prior:
            claimed.add(prior["id"])
            repost_count = prior.get("repost_count", 0) + 1
            # prefer the new posting's own exact date over "the day we happened to scrape it" -
            # they only diverge when a run is missed or errors out, but when they do, the actual
            # date is the more honest one to keep permanently in the record
            repost_of, reposted_on = prior["id"], (j.get("posted") or today)
            # walk-free lineage tracking: inherit the root/earliest-date from the prior entry so a
            # long repost chain never needs to be re-walked to answer "how old is this role really"
            lineage_root = prior.get("lineage_root") or prior["id"]
            lineage_first_seen = prior.get("lineage_first_seen") or prior.get("first_seen") or today
            if spons != "Blocked" and prior.get("state") in ("dismissed", "applied", "shortlisted"):
                entry_state = prior["state"]  # a repost of a role Yazad already acted on inherits that decision
            if not prior.get("closed_on"):
                prior["closed_on"], prior["closed_reason"] = today, "reposted"

        posted = j.get("posted") or ""
        # provenance for the date, surfaced on the tracker page so a date never looks more certain
        # than it is: every non-Workday ATS returns an exact native timestamp from its list call
        # (see the per-fetcher functions above), so a posted value there is always "exact". Workday
        # sets posted_source itself via describe()/_workday_detail_posted (exact startDate vs the
        # approx relative-text fallback) - if describe() never got that far (threw, or this entry
        # skipped it), fall back to "approx" since workday()'s own bulk-listing value is always the
        # relative-text parse.
        posted_source = j.get("posted_source") or (("exact" if c["ats"] != "workday" else "approx") if posted else "")
        entry = dict(id=eid, company=name, role=j["title"], location=j["location"], link=j["url"],
                     posted=posted, posted_source=posted_source, posted_reverified=False,
                     description_status="failed" if j.get("desc_failed") else "ok",
                     site=j.get("site"),  # which Workday site this came from, for multi-site companies
                     source="job-scout", first_seen=today,
                     sponsorship_status=spons, sponsorship_evidence=evidence,
                     experience=experience, experience_evidence=experience_evidence,
                     salary=salary, salary_evidence=salary_evidence,
                     kw_hits=keyword_hits(j["title"], text, keywords),
                     state=entry_state, first_run=first_run,
                     last_seen_live=today, miss_count=0, closed_on=None, closed_reason=None,
                     repost_count=repost_count, repost_of=repost_of, reposted_on=reposted_on,
                     lineage_root=lineage_root, lineage_first_seen=lineage_first_seen,
                     ghost_status="Unclear", ghost_evidence="")  # refreshed queue-wide by _classify_ghosts()
        queue.append(entry)
        queued_ids.add(eid)
        if prior:
            counts["reposts"] += 1
        if spons == "Blocked":
            counts["blocked"] += 1
            continue
        if spons == "OPT Only":
            counts["opt_only"] = counts.get("opt_only", 0) + 1  # listed in its own section, no alert
            continue
        counts["queued"] += 1
        # first run seeds the queue silently; a repost only alerts if it flips from blocked to open -
        # otherwise it's the same decision Yazad already made, just resurfacing under a new id
        if not first_run and not silent and (not prior or prior.get("state") == "auto_blocked"):
            if _alert_cap_reached(counts):
                counts["alert_capped"] = counts.get("alert_capped", 0) + 1
            else:
                counts["alert_tries"] = counts.get("alert_tries", 0) + 1
                if notify(cfg.get("notify", {}), entry, args.dry_run):
                    counts["alerted"] += 1
                elif not args.dry_run:
                    entry["alert_pending"], entry["alert_attempts"] = True, 1  # retried next run by _retry_pending_alerts()
                    counts.setdefault("failed_now", set()).add(eid)  # not retried again within this same run
    if not args.dry_run and (jobs or not first_run):
        # a first fetch that returned nothing must not seed state: once the config is fixed, every match would alert
        state[name] = sorted(seen | {j["id"] for j in jobs})  # union: a capped search must not re-alert on drift
    elif first_run:
        print(f"[dry-run] {name}: first run, would queue {len(fresh)} current matches silently")


def _alert_cap_reached(counts):
    """The cap budgets delivery attempts (successful or not); a delivered alert always counts as an attempt too."""
    return max(counts.get("alerted", 0), counts.get("alert_tries", 0)) >= ALERT_CAP_PER_RUN


def _retry_pending_alerts(queue, notify_cfg, counts, dry):
    """Re-sends alerts that failed earlier, under the per-run cap; gives up after ALERT_MAX_ATTEMPTS so a
    dead ntfy topic cannot retry forever. Closed or acted-on entries are dropped from the retry list."""
    for e in queue:
        if not e.get("alert_pending") or e["id"] in counts.get("failed_now", ()):
            continue
        if e.get("closed_on") or e.get("state") != "new" or e.get("alert_attempts", 1) >= ALERT_MAX_ATTEMPTS:
            e["alert_pending"] = False
            continue
        if _alert_cap_reached(counts):
            continue  # keep walking: later rows may still need their stale flag cleared
        counts["alert_tries"] = counts.get("alert_tries", 0) + 1
        if notify(notify_cfg, e, dry):
            e["alert_pending"] = False
            counts["alerted"] += 1
        elif not dry:
            e["alert_attempts"] = e.get("alert_attempts", 1) + 1


def _recall_company(c, name, hits, seen, queued_ids, tombstones, pool, queue, keywords, cfg, args, counts):
    """--recall only: queue roles that match the filters now, were fetched before (so are in `seen`) yet never
    reached the queue, e.g. because the filters were narrower then. Silent, tagged origin=recall, capped, and it
    skips tombstoned ids and anything old enough to be pruned straight away."""
    cutoff = (date.today() - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    todo = []
    for j in hits:
        eid = f"{name}:{j['id']}"
        if j["id"] not in seen or eid in queued_ids or eid in tombstones:
            continue
        if (j.get("posted") or "")[:10] and j["posted"][:10] < cutoff:
            continue
        todo.append(j)
    room = RECALL_MAX - counts.get("recalled", 0)
    todo = todo[:max(room, 0)]
    if not todo:
        return
    counts["recalled"] = counts.get("recalled", 0) + len(todo)
    print(f"[recall{' dry-run' if args.dry_run else ''}] {name}: {len(todo)} matching roles never queued")
    if args.dry_run:
        return
    descs = {}
    for j, fut in [(j, pool.submit(describe, c, j)) for j in todo]:
        try:
            text, posted, posted_source = fut.result()
            descs[j["id"]] = text
            if posted:
                j["posted"], j["posted_source"] = posted, posted_source
        except Exception as e:  # noqa: BLE001
            print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
            descs[j["id"]] = ""
    before = len(queue)
    _process_company(c, name, todo, descs, True, [], True, seen, {}, queue, queued_ids, keywords, cfg, args, counts, silent=True)
    for e in queue[before:]:
        e["origin"] = "recall"


def _send_summaries(notify_cfg, floods, counts, dry):
    """One alert per flooded company, plus one for roles past the per-run alert cap."""
    lines = [f"{name}: {n} roles appeared at once, queued silently (flood guard)" for name, n in floods]
    if counts.get("alert_capped"):
        lines.append(f"{counts['alert_capped']} more new roles queued without individual alerts (cap {ALERT_CAP_PER_RUN})")
    path = HOME / "pending_summaries.json"
    pending = [] if dry else load_json(path, [])
    todo = pending + [dict(line=line, attempts=0) for line in lines]
    still = []
    for item in todo:
        ok = notify(notify_cfg, dict(company="Summary", role=item["line"], location="", sponsorship_status="n/a", kw_hits=[],
                                     link=notify_cfg.get("tracker_url") or "https://ntfy.sh"), dry)
        if not ok and not dry and item["attempts"] + 1 < ALERT_MAX_ATTEMPTS:
            still.append(dict(line=item["line"], attempts=item["attempts"] + 1))
    if not dry and (still or pending):
        HOME.mkdir(parents=True, exist_ok=True)
        atomic_json.write(str(path), still)


def _still_listed(c, e):
    """Closure double-check for term-search boards (Workday, Oracle, Eightfold): True unless the posting's own
    detail lookup definitively says it is gone. A list miss alone must not close it, because a keyword
    search can drop a live posting. Any failure answers True so an unverifiable posting stays open."""
    ats = c.get("ats")
    if ats == "workday":
        return _workday_still_listed(c, e)
    pid = e["id"].split(":", 1)[1]
    try:
        if ats == "oracle":
            d = http(f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
                     + urllib.parse.urlencode({"onlyData": "true", "finder": f"ById;Id={pid}"}))
            return bool(d.get("items"))
        if ats == "eightfold":
            _eightfold_detail(c, pid)
            return True
        if ats == "smartrecruiters":
            http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{pid}")
            return True
    except urllib.error.HTTPError as ex:
        return ex.code not in (404, 410)
    except Exception:
        return True
    return True


def _workday_still_listed(c, e):
    """True when a requisition the list diff missed is still on the Workday board (searched by its req id).
    Keyword-term searches occasionally drop a live posting, so a miss alone must not close it. Any
    failure answers True: an unverifiable posting stays open rather than being closed on a guess."""
    path = e["id"].split(":", 1)[1]
    tok = re.search(r"_([A-Za-z0-9\-]+)$", path)
    if not tok:
        return True
    req = tok.group(1)
    if re.match(r"^[A-Za-z]*-?\d+-\d+$", req):
        req = req.rsplit("-", 1)[0]  # a trailing -N is a repost suffix, the search index holds the base req id
    site = e.get("site") or (c["site"][0] if isinstance(c.get("site"), list) else c.get("site"))
    try:
        d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}/jobs",
                 data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": req})
        return any(req in (p.get("externalPath") or "") for p in d.get("jobPostings", []))
    except Exception:
        return True


LIVENESS_WORKERS = 8
LIVENESS_BUDGET_SECONDS = 420
_liveness_started = []


def _still_listed_budgeted(c, e):
    """_still_listed, or None once the run's probe time budget is spent (caller keeps the row open)."""
    if not _liveness_started:
        _liveness_started.append(time.monotonic())
    if time.monotonic() - _liveness_started[0] > LIVENESS_BUDGET_SECONDS:
        return None
    return _still_listed(c, e)


def _update_liveness(name, jobs, complete, queue, counts, c=None):
    """Detects closed/reopened postings for one company's queue entries by diffing against the FULL
    current listing (`jobs`, not the title/location-filtered `hits`) - so editing filters never closes
    an entry. Closing requires CLOSE_AFTER_MISSES consecutive complete (unpaginated-cap) runs without
    the id showing up, since a single miss on a paginated/capped board just means it fell outside the
    search window, not that the posting is gone."""
    if not jobs:
        print(f"! {name}: empty listing, skipping close/reopen check (broken slug or API?)", file=sys.stderr)
        return
    live_ids = {j["id"] for j in jobs}
    listed_posted = {j["id"]: j.get("posted") or "" for j in jobs}
    today = date.today().isoformat()
    mine = [e for e in queue if e["company"] == name]
    open_mine = [e for e in mine if not e.get("closed_on")]
    missing = [e for e in open_mine if e["id"].split(":", 1)[1] not in live_ids]

    for e in mine:
        if e["id"].split(":", 1)[1] not in live_ids:
            continue
        e["last_seen_live"] = today
        e["miss_count"] = 0
        if c and c.get("ats") == "workday":
            _note_possible_refresh(e, c, listed_posted.get(e["id"].split(":", 1)[1], ""))
        if e.get("closed_on") and e.get("closed_reason") != "manual":  # same id came back: a real reopen
            e["closed_on"], e["closed_reason"] = None, None
            e["repost_count"] = e.get("repost_count", 0) + 1
            e["reposted_on"] = today
            counts["reopened"] += 1

    # A mass vanish (likely a broken fetch, bad slug, API change) needs stronger evidence before
    # closing anything - but must not block closure forever: raising the bar rather than returning
    # early still lets miss_count climb, so a genuine mass closure eventually goes through instead of
    # permanently freezing every entry the moment the ratio is first crossed.
    mass_vanish = len(open_mine) >= 3 and len(missing) / len(open_mine) > MASS_VANISH_GUARD
    if mass_vanish:
        print(f"! {name}: {len(missing)}/{len(open_mine)} open queue entries vanished at once, "
              f"requiring stronger evidence before closing (possible broken fetch)", file=sys.stderr)
    if not complete:
        return  # a truncated/capped listing can't prove absence - a job outside the window isn't closed
    close_after = CLOSE_AFTER_MISSES * 3 if mass_vanish else CLOSE_AFTER_MISSES
    checkable = bool(c and c.get("ats") in ("workday", "oracle", "eightfold", "smartrecruiters"))
    due = [e for e in missing if e.get("miss_count", 0) + 1 >= close_after and (e.get("last_seen_live") or "") < today]
    listed = {}
    if checkable and due:
        # sequential probes across thousands of vanished rows ran past the job timeout and cancelled every run
        with ThreadPoolExecutor(LIVENESS_WORKERS) as ex:
            futs = {e["id"]: ex.submit(_still_listed_budgeted, c, e) for e in due}
            listed = {k: f.result() for k, f in futs.items()}
    for e in missing:
        e["miss_count"] = e.get("miss_count", 0) + 1
        # hourly runs make two misses only two hours: also require the last confirmed sighting to be before today
        if e["miss_count"] >= close_after and (e.get("last_seen_live") or "") < today:
            verdict = listed.get(e["id"], False)
            if verdict is None:  # probe budget spent: leave it open and retry next run
                e["miss_count"] = close_after - 1
                continue
            if verdict:
                e["last_seen_live"], e["miss_count"] = today, 0  # live but dropped out of the keyword searches
                continue
            e["closed_on"], e["closed_reason"] = today, "removed"
            counts["closed"] += 1


# data/filters_sig.json: 


def _filters_signature(filters):
    return hashlib.sha1(json.dumps(filters, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _load_tombstones():
    path = HOME / "removed.jsonl"
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["id"])
        except (ValueError, KeyError):
            continue
    return out


def _write_tombstones(ids, reason):
    """Append-only record of queue entries removed on purpose, so --recall never resurrects them."""
    if not ids:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    with open(HOME / "removed.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for i in sorted(ids):
            f.write(json.dumps(dict(id=i, reason=reason, on=today)) + "\n")


def _prune_stale(queue, counts, unverified=frozenset(), configured=None):
    """Drops "new"/"auto_blocked" entries whose employer-posted date is older than
    PRUNE_AFTER_DAYS, so queue.json (and the HTML page rendering it) doesn't grow forever. Never
    touches shortlisted/applied/dismissed - those came from an explicit `--mark`, a human decision
    the scout has no business overriding just because time passed. An entry with no cleanly-parsed
    posted date is kept rather than guessed at. Entries of `unverified` companies (fetch failed, shard
    missing, truncated or empty listing this run) are kept too: absence from a bad fetch proves nothing."""
    cutoff = (date.today() - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    kept = []
    pruned = 0
    for e in queue:
        posted = e.get("posted") or ""  # null dates exist in the queue; len(None) would crash the prune
        # an old posting still on the employer's board today is live (employers bump/relist), so keep it
        still_live = e.get("last_seen_live") == date.today().isoformat() and not e.get("closed_on")
        is_stale = (e["state"] in ("new", "opt_only", "auto_blocked") and len(posted) >= 10 and posted[:10] < cutoff
                    and not still_live and e["company"] not in unverified
                    and (e.get("closed_on") or (configured is not None and e["company"] not in configured)))
        if is_stale:
            pruned += 1
            counts.setdefault("pruned_ids", []).append(e["id"])
        else:
            kept.append(e)
    counts["pruned"] = pruned
    queue[:] = kept


def main():
    ap = argparse.ArgumentParser(description="Free ATS job discovery, standalone (no tracker dedup).")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recall", action="store_true", help="also queue matching roles that were fetched before but never queued (silent, capped, manual)")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --queue: include every state")
    ap.add_argument("--json", action="store_true", help="with --queue: JSON output")
    ap.add_argument("--days", type=int, help="with --queue: only postings posted within the last N days")
    ap.add_argument("--html", metavar="PATH", help="with --queue: write a clickable HTML page instead of stdout")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATE"))
    ap.add_argument("--fetch-shard", metavar="I/N", help="fetch only shard I of N and write --out, no state changes")
    ap.add_argument("--out", metavar="PATH", help="with --fetch-shard: result file")
    ap.add_argument("--from-fetched", metavar="DIR", help="run using shard result files instead of fetching")
    args = ap.parse_args()
    if args.fetch_shard:
        return cmd_fetch_shard(args)
    if args.init:
        return cmd_init()
    if args.queue:
        return cmd_queue(args)
    if args.mark:
        args.id, args.state = args.mark
        return cmd_mark(args)
    run(args)


if __name__ == "__main__":
    main()
