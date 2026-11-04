# Live diagnosis of the last scout warnings

Author: Yazad Madan
Verified: October 3, 2026 (America/New_York)

Source run: [37128573750](https://github.com/DarkKnight6499/job-scrap/actions/runs/37128573750), started at 10:08:06 AM Eastern. `gh run view 37128573750 --log` confirms all seven requested empty-listing warnings and Wellington's 11/17 warning at 10:10 AM Eastern. The downloaded shard artifacts mark every empty board complete=True with err=None; Wellington has 122 results, complete=False, err=None. No configuration, queue or scout changes were made for this diagnosis.

## Ariel Investments

**Diagnosis: genuinely empty published board.** The configured Greenhouse API returns HTTP 200, jobs=[], meta.total=0. The employer-branded [Ariel board](https://job-boards.greenhouse.io/arielinvestments), in its Current openings section, independently states that there are no current openings. The slug identifies Ariel Investments correctly. No parser failure or live replacement board was established.

## Lone Pine Capital

**Diagnosis: genuinely empty published board.** The configured API returns HTTP 200 with jobs=[], meta.total=0. The [Lone Pine Capital LLC board](https://job-boards.greenhouse.io/lonepinecapital), in its Current openings section, shows the correct employer name and no current openings. This is not a wrong-company slug or a parsing failure. No move was verified.

## Marshall Wace

**Diagnosis: a valid but insufficient board selection.** The configured [marshallwace board](https://job-boards.greenhouse.io/marshallwace) is employer-branded and currently empty, matching the raw API. However, the employer's [Technology Early Talent Programme page, Graduates section](https://www.mwam.com/technology-early-talent-scheme/) links its application button to [mw-tech-grad](https://job-boards.greenhouse.io/mw-tech-grad), which publishes six live graduate/associate roles. That board's API also returns six jobs. The existing slug is not an invalid parser input, but it misses this active recruitment channel. Coverage of experienced-hire channels was not established by this check.

## Franklin Templeton

**Diagnosis: current discovery front end is Phenom; the configured Workday listing no longer exposes the live jobs.** The configured host, tenant and Primary-External-1 site are valid. Blank, treasury and risk POST searches all return HTTP 200 with total=0 and jobPostings=[]. The [official careers search](https://careers.franklintempleton.com/jobs?s=1) is a Phenom site and embeds live positions with apply URLs on that exact Workday host/site. One sample, [Alternative Specialist, requisition 868649](https://franklintempleton.wd5.myworkdayjobs.com/Primary-External-1/job/Tokyo-Japan/Alternative-Specialist_868649), has live JobPosting JSON-LD, and its Workday detail endpoint returns posted=true and the same title. Thus the tenant/site is not wrong and the parser is faithfully reading an empty response. Workday remains the application/detail backend; the discovery route needs reassessment. The historical date or cause of the listing change is unverified.

## Millennium Management

**Diagnosis: discovery has moved to Eightfold.** The configured Workday mlpcareers listing returns total=0 for blank, treasury and risk searches. The employer's [Careers page, Open roles button](https://www.mlp.com/careers/) links to mlp.eightfold.ai and redirects to [career.mlp.com](https://career.mlp.com/careers?domain=mlp.com). Its public `/api/apply/v2/jobs?domain=mlp.com&start=0&num=10&query=` endpoint returns count=214 and populated positions, including Identity Security Engineer. The existing Workday parser is not dropping populated rows; its source is empty. The live replacement is verified, without changing config.

## ClearAlpha Technologies

**Diagnosis: empty configured Ashby board; no replacement verified.** The [configured posting API](https://api.ashbyhq.com/posting-api/job-board/clearalpha) returns HTTP 200 with jobs=[] and apiVersion=1. A deliberately nonexistent board returns HTTP 404, so this is not the API's general missing-slug response. The public clearalpha page is a generic Jobs shell and does not establish employer ownership. The employer's [Join Our Team page, contact invitation](https://www.clearalphatech.com/join) offers direct contact and supplies no replacement ATS link. No parsing failure is present in the observed API payload. Whether this Ashby board is still the employer's intended recruitment channel is **unverified**; the evidence does not justify asserting a new slug or a migration.

## Sculptor Capital Management

**Diagnosis: genuinely empty current Workday board.** Blank, treasury and risk requests return total=0 with correctly shaped empty jobPostings. The employer's [Careers page, See Open Opportunities button](https://www.sculptor.com/careers) still links directly to the configured Sculptor_External_Career_Site on sculptor.wd12.myworkdayjobs.com. The current source is correct and empty. Historical indexed internship pages do not establish that those requisitions are open now. No move or parser failure was verified.

## Wellington Management

**Diagnosis: evidence favors withdrawal of a Co Op cohort; the board and parser remain functional.** The last run's warning does not prove a broken fetch: its shard already says complete=False. A live unfiltered [Workday listing](https://wellington.wd5.myworkdayjobs.com/External) reports 124 postings. Fetching all pages in increments of 20 produced 124 distinct external paths. Comparing the 17 open queue rows from the reviewed snapshot against the archived shard reproduces the 11 missing rows. All 11 are also absent from the complete live listing, including a comparison by stable requisition suffix rather than title/path spelling. The board remains populated, and no renamed path for these requisitions was found.

The missing cohort comprises Treasury Operations, Global Risk & Performance Strategy, two Legal/Compliance/Risk roles, Global Third Party Risk, Trading Infrastructure Risk/Bond Forward, Trading Infrastructure and Risk/Alert Specialist, Trading Infrastructure and Risk/Currency Forward Specialist, Code of Ethics/Legal/Compliance/Risk, and Trade Coordination roles in Fixed Income and Equity. Their IDs are R94825-1, R94868-1, R94835-1, R94836-1, R94837-1, R94823-1, R94819-1, R94821-1, R94845-1, R94843-1 and R94824-1.

All 11 old detail endpoints returned HTTP 403. That response alone does not confirm closure or its cause. Their absence from the full live inventory is verified; interpreting the concentrated Co Op disappearances as a cohort withdrawal is an inference. No wrong tenant, moved board or parser-shape failure was found. The production incomplete flag correctly prevents miss increments and pruning in this run, even though the mass-vanish warning is printed before the incomplete-listing guard.

## Verification method

Compared saved run logs and downloaded shard JSON with direct live ATS responses and employer-owned careers pages. Opened the primary pages above with web browsing and direct HTTP requests, including raw HTML where the browser extractor omitted job data. Where employer pages blocked direct requests, browsed their primary page or followed its own application links. No third-party job aggregators were used to declare a replacement endpoint. The remaining ClearAlpha ownership uncertainty and Wellington closure-cause uncertainty are explicitly marked.
