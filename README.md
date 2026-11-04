# job-scrap

Free job discovery: polls public ATS job-board APIs (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday, Oracle Recruiting Cloud, Personio, SuccessFactors RSS, iCIMS, Eightfold, Goldman Sachs's own site) for a curated
list of companies, filters by title/location keywords, flags explicit no-sponsorship
language in the description, and pushes a phone alert (ntfy) for anything
new. No paid scraping API, no LLM tokens.

Runs on a schedule via GitHub Actions (`.github/workflows/scout.yml`, every 30 min)
- no server or laptop required. State (`data/state.json`, `data/queue.json`)
is committed back to the repo after each run so the next run picks up where
the last one left off.

Every run also diffs each company's live listing against its queued entries:
a posting missing for two consecutive complete (unpaginated-cap) runs is
marked closed, a same-id reappearance is reopened, and a new id matching a
closed entry's (title, location) is recorded as a repost of it - alerting
again only if the prior posting had been auto-blocked and the new one isn't.

**No application-tracker integration.** Unlike a private companion version,
this build doesn't know what you've already applied to.

## Setup

1. Add a repo secret `NTFY_TOPIC` (Settings -> Secrets and variables ->
   Actions) with your own [ntfy.sh](https://ntfy.sh) topic string. Subscribe
   to that topic in the ntfy phone app to receive alerts. The topic is
   deliberately not committed to `data/config.json` since this repo is
   public - anyone who saw it in the repo could subscribe to your alerts.
   Each alert's main tap opens that job's own posting; a secondary
   "Open Jobs Tracker" button (`notify.tracker_url` in `data/config.json`)
   opens the full shared queue page instead.
2. Edit `data/config.json` - `companies` (each needs `ats` +
   the fields that ATS needs: `slug` for greenhouse/ashby/smartrecruiters/
   personio (the company subdomain in `https://<slug>.jobs.personio.de`),
   `host`/`tenant`/`site`/`search` for workday, `host`/`site`/`search` for
   oracle) and `filters` (`title_include`/`title_exclude`/`locations_include`).
3. Trigger a manual run from the Actions tab (`workflow_dispatch`) to confirm
   it works before waiting for the schedule.

## Sharing the queue (GitHub Pages)

Every scheduled run also regenerates `docs/index.html` and commits it, so the
same queue can be shared with someone else via a stable URL instead of
sending them a file. One-time setup: repo Settings -> Pages -> Source:
"Deploy from a branch" -> branch `main`, folder `/docs`. The page includes a
live client-side search box (comma = OR, space = AND) so different people
can filter the same shared data down to their own interests - nothing is
sent anywhere and each browser remembers only its own last search.

## Local use

```
python job_scout.py --check      # per-company job/match counts, no state change
python job_scout.py --dry-run    # show what would queue/alert, no state change
python job_scout.py              # normal run
python job_scout.py --queue      # show the triage queue
python job_scout.py --queue --all --html queue.html   # write the same page written to docs/index.html
```

## Dedup against your own application tracker (optional, local only)

The scheduled scraper itself has no application-tracker integration (see above) - it doesn't
know what you've applied to, on purpose, since this repo is public. `dedup_applied.py` +
`identity_lib.py` are an optional add-on that runs entirely on your own machine: point it at
your own tracker (a `.csv` or `.xlsx`, one row per application) and it writes a local HTML copy
of `data/queue.json` with anything you've already applied to filtered out. Nothing from your
tracker is ever written back into `data/`, committed, or sent anywhere.

**Setup:**

1. Clone or fork this repo and make sure you can already run `python job_scout.py` locally
   (see Local use above) - `dedup_applied.py` reads `data/queue.json` that produces.
2. Open `dedup_applied.py` and edit the `CONFIG` block at the top:
   - `APPLICATIONS_FILE` - path to your tracker (`.csv` needs no extra dependency; `.xlsx`
     needs `pip install openpyxl`).
   - `COMPANY_COL`, `ROLE_COL`, `LOCATION_COL`, `LINK_COL` - your tracker's actual column
     headers (set `LINK_COL = None` if you don't track a link per row).
   - `STATUS_COL` and `APPLIED_STATUSES` - which column holds a status, and which values in
     it mean "I've applied" (set `STATUS_COL = None` to treat every row in the file as applied).
   - `OUTPUT_HTML` - where to write the result (defaults to `job_scrap_dedup.html` next to
     this script; already covered by `.gitignore` so it's never committed by accident).
3. Run it:
   ```
   python dedup_applied.py
   ```
   It prints how many tracker rows it loaded and how many queue entries were kept vs. dropped,
   then writes `OUTPUT_HTML` - open that file directly in a browser.
4. Re-run it any time after `job_scout.py` updates `data/queue.json` (for example, on the same
   30-min schedule as the scraper via your OS's task scheduler / cron) to keep the view current.

Matching logic (`identity_lib.py`): a queue entry counts as "already applied" if its Link
matches one of your rows' Link, or - when either side has no Link - if Company + Role Title +
Location match. This is the same fallback the private companion version uses, so results are
identical to it when pointed at the same tracker.
