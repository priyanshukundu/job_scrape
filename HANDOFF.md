# Handoff: open items after the 2026-10-11/12 session

Everything done in the session is on `main`. Nothing is unpushed. This file lists only what is still open, with enough context to pick each item up cold.

## 1. Housekeeping (needs repo access this session lacked)

- Delete the merged remote branches `backfill-script`, `fix-describe-empty` and `global-title-terms` (a `git push origin --delete ...` got HTTP 403 from the session proxy). All three are fully merged into `main`. Alternative: turn on "Automatically delete head branches" in the repo settings.
- Local scratch: `.claude/worktrees/*` folders from the agents are untracked scratch space and can be deleted (`git worktree prune` after removing them).

## 2. Chips still to clean (user agreed these are mostly not relevant; same method as the retail, admin/HR, sales and operations passes)

Method used each time: find the dominant title phrases, check each phrase against every queue row for finance collateral (never exclude titles that hit a non-"associate" `title_include` term or the treasury, ALM, credit, market, model, quant, operational-risk chips without listing them first), add the vetted phrases to `filters.title_exclude`, remove only the matching `new` and `opt_only` queue rows, keep survivors byte-identical, add a small test.

Open chips, with New+opt_only counts at the time of the last measurement:

| Chip | Rows | Note |
|---|---|---|
| Other (no category) | about 1,440 | Mostly non-finance employers. About 375 rows come from 15 companies (Highmark Health, Thermo Fisher, Carrier, Merck, Amgen, IQVIA, Baxter, Vertex, AstraZeneca, Regeneron, Bristol Myers Squibb...). Best fix is company-level: drop those non-finance employers from `data/config.json`. |
| Insurance/Actuarial | about 101 | actuary, claims, annuity pricing |
| Wealth/Trust | about 176 | private bank, private client, financial advisor |
| Accounting/Controllership/Tax | about 146 | `tax` and `fund accounting` are already excluded |
| Early Careers/Internships | about 140 | user not yet decided: `intern` is already excluded; "2027 Analyst" and graduate programs remain |
| Retail and branch roles inside Banking/Lending/Underwriting | part of about 529 | community banker, branch manager, insurance underwriting; keep commercial lending |
| Technology/Data, Compliance/Audit, Product/Program/Strategy, Private Markets/Real Estate/Structured, Investment Banking/M&A | n/a | judged mixed; user has not decided |

Already done: Legal/HR/Marketing/Admin (chip removed), Sales/Business Development (kept capital introduction, capital formation, national accounts, consultant relations on request), Operations/Payments, retail store roles.

Known leftovers from the operations pass: about a dozen bare "Operations Associate" titles remain because excluding the phrase would also hit "Treasury Operations Associate". Fixing that needs a code-level treasury carve-out. Fixed Income trader rows at RBC with "Business Development" in the title were kept.

Chips are now 24 including "Other".

## 3. New fetchers requested by the career-page research (not built)

| Firm | ATS | Detail |
|---|---|---|
| Chevy Chase Trust | Lever, shared board `bfsaul` (B.F. Saul Company) | Needs a `team` filter on the lever fetcher: filter `team == "Chevy Chase Trust"` (4 of 22 postings). Lever `createdAt` gives real dates. |
| Derivative Path | JazzHR | `derivativepath.applytojob.com`, about 5 postings; date support unchecked. |
| BestEx Research | Workable | Slug unverified (Cloudflare 1015 blocked `apply.workable.com` from the sandbox). |
| Osaic | NAS Recruitment | `careers.osaic.com`, about 66 jobs, listed in `https://careers.osaic.com/jobs-site.xml` with `lastmod`. |
| LHH | Radancy TalentBrew on `careers.adeccogroup.com/en/lhh-jobs` | Works but returns all of Adecco (889 jobs, about 114 LHH) and has no posted dates. Needs a keyword/filter option. |

No public board found: CIFC (LinkedIn link only), Bluesky Capital (static page), Optimal Blue (LinkedIn link only), Lighthouse Investment Partners (no careers page), NatWest (Cloudflare Turnstile; Workday guesses 422). Unclear: BayBridge (no finance firm found), Cornell (no Cornell Capital board found; Cornell University not checked).

Other unreachable: BNP Paribas (Akamai on the group site; the applicant system `bwelcome.hr.bnpparibas` serves job pages but has no list; about 60 US roles estimated, about 9,000 requests per run to enumerate ids), Citadel and NatWest (Cloudflare Turnstile), Rabobank (WAF 403). UniCredit (497 jobs, none in the US) and ABN AMRO (72 of 134 jobs reachable, no US) were skipped as no US hiring. Their reasons are recorded in `data/unsupported.json`.

## 4. Known limits

- **Citi (Workday)** still reports `complete=False`: facet sums disagree (4,163 / 4,195 / 4,206), so full coverage cannot be proven. Two live postings are missed by the facet slices, including "US Rates Strategist (Treasury Trading)". Possible fix: whole-board read plus a targeted title-term search for Citi only. Closure detection stays off for Citi.
- **Two Sigma** returned a one-off HTTP 406 in a full run (works alone; watch it).
- **Undated postings** (they fall back to first_seen): Zions, D. E. Shaw, Oppenheimer, and Cincinnati Financial jobs older than 28 days. No date source exists on those sites (documented attempts in the session).
- **Approximate dates** ("~" on the page, kept on purpose): Credit Agricole CIB, East West Bank and UBS (site update dates), Cincinnati Financial (age-bucket midpoint, under 28 days), ADP RM (Valley, BankUnited: last-posted time). `date_from_id` (job-number date estimate) is implemented but off for every company because job numbers do not track dates.
- **Doubtful dates**: ConnectOne (list date and page date disagree on 2 of 4 jobs) and about 16 TowneBank evergreen postings dated 2026-06-30.
- **No describe route** for a few ATS types when copying JDs: Phenom, Ashby and a handful of others (about 93 queue rows during the salary backfill).
- **Never guess**: experience is read only from the description (no title inference) and an unlabelled small pay pair is not assumed hourly. A few salaries show the widest span of a multi-range location table, with a note in the evidence.

## 5. Not done / ideas

- If 24 chips is still too many: merge Quant/Modeling/Pricing into Model & Quantitative Risk (a "valuation control" exclusion on Quant/Modeling/Pricing blocks a plain merge; see `test_role_categories.py`).
- Salary and experience for rows with no describe path (Goldman list-derived text, Lever, Ashby) could use the list functions in a second backfill pass.
- The hosted page (`docs/index.html`) is regenerated by the scout; it shows the new chips, queue cleanups and parsed values the next time the scout runs.

## 6. Where things are

- Filters and companies: `data/config.json`. Queue: `data/queue.json`. State: `data/state.json` (not edited by hand).
- Role chips: `ROLE_CATEGORIES` and `ROLE_CATEGORY_EXCLUDE` in `job_scout.py`; tests in `test_role_categories.py` (frozen pre-merge copy in `role_categories_premerge.py`).
- Backfill: `backfill_salary_experience.py` (`--stated-only --apply`; already applied once).
- Big-board reads: `workday()` probe, whole-board and facet-slice paths, and `oracle()` whole-board read, with `test_big_board_read.py`.
- Validation: `python validate_config.py` and `python -m pytest -q` (582+ tests, all passing at the last push).
