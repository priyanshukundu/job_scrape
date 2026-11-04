# Shared ATS endpoint ownership report

Author: Yazad Madan
Verified: October 3, 2026 (America/New_York)

This is a read-only audit. No company configuration, queue rows, or scout code were changed for this report. Counts include all queue states, including closed rows. These are affected rows, not a count of unique requisitions or confirmed bad labels.

Method: read data/config.json and data/queue.json, run validate_config.validate, fetch each shared Workday listing, resolve one live posting per configured name, and open its canonical public posting URL. All 21 names resolved to live detail records with posted=true; the 16 distinct canonical posting pages returned HTTP 200 and JobPosting JSON-LD. Employer attribution uses hiringOrganization.name and the posting description, rather than the configured company label. When a name has no queue rows, its sample comes from the shared live listing. Aliases can therefore cite the same posting.

Validation: 10 shared-endpoint groups affecting 334 queue rows. No missing required fields or normalized duplicate company names were found. The new validator exits 1 on these collisions, so CI will block scouting until they are resolved. No remediation was applied.

## Booz Allen, Booz Allen Hamilton

Endpoint: `workday/bah.wd1.myworkdayjobs.com/bah/BAH_Jobs`. Queue rows: 60.

Board attribution: **Booz Allen Hamilton**. Both names lead to the same requisition. The hiringOrganization.name field identifies Booz Allen Hamilton, and the compensation section names Booz Allen. This is an alias collision.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Booz Allen | 30 | [Audit Support Financial Analyst](https://bah.wd1.myworkdayjobs.com/BAH_Jobs/job/Honolulu-HI/Audit-Support-Financial-Analyst_R0240062) | 631 Booz Allen Hamilton_United States |
| Booz Allen Hamilton | 30 | [Audit Support Financial Analyst](https://bah.wd1.myworkdayjobs.com/BAH_Jobs/job/Honolulu-HI/Audit-Support-Financial-Analyst_R0240062) | 631 Booz Allen Hamilton_United States |

## Cadence Bank, Cadence Design Systems

Endpoint: `workday/cadence.wd1.myworkdayjobs.com/cadence/External_Careers`. Queue rows: 2.

Board attribution: **Cadence Design Systems**. Both names lead to the same technology-company posting. The posting discusses technology and supplies staffing@cadence.com as its accommodation contact. The official Cadence careers page uses that same contact and identifies Cadence Design Systems in its footer. Cadence Bank is incorrectly assigned to this board.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Cadence Bank | 1 | [Senior Financial Analyst](https://cadence.wd1.myworkdayjobs.com/External_Careers/job/SAN-JOSE/Senior-Financial-Analyst_R55982) | (not supplied) |
| Cadence Design Systems | 1 | [Senior Financial Analyst](https://cadence.wd1.myworkdayjobs.com/External_Careers/job/SAN-JOSE/Senior-Financial-Analyst_R55982) | (not supplied) |

Additional primary source: [Cadence careers, accommodation contact and copyright footer](https://www.cadence.com/en_US/home/company/life-at-cadence/careers.html).

## Carlyle Group, Carlyle

Endpoint: `workday/carlyle.wd1.myworkdayjobs.com/carlyle/Carlyle`. Queue rows: 22.

Board attribution: **The Carlyle Group**. Both samples identify THE CARLYLE GROUP EMPLOYEE CO., LLC in hiringOrganization.name. The Global Credit sample also names The Carlyle Group in its opening paragraph. Carlyle and Carlyle Group are aliases here.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Carlyle Group | 9 | [2-Year Associate, Global Credit Management](https://carlyle.wd1.myworkdayjobs.com/Carlyle/job/New-York-NY/Associate---2-Year_R-00275) | 1P284 THE CARLYLE GROUP EMPLOYEE CO., LLC |
| Carlyle | 13 | [Engineer, Treasury & Cash Management](https://carlyle.wd1.myworkdayjobs.com/Carlyle/job/Washington-DC/Engineer--Treasury---Cash-Management_R-00119-1) | 1P284 THE CARLYLE GROUP EMPLOYEE CO., LLC |

## LCH, LSEG, London Stock Exchange Group

Endpoint: `workday/lseg.wd3.myworkdayjobs.com/lseg/Careers`. Queue rows: 13.

Board attribution: **LSEG (London Stock Exchange Group)**. The About Us section identifies LSEG as London Stock Exchange Group. The two New York samples identify London Stock Exchange Group Holdings Inc; the fallback sample for LCH identifies LSEG Business Services RM S.R.L. This is a broad LSEG board, not a board restricted to LCH. LCH has no queue rows, so its sample comes from the live shared listing.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| LCH | 0 | [Senior Software Engineer C++](https://lseg.wd3.myworkdayjobs.com/Careers/job/Bucharest---Iuliu-Maniu-Boulevard/Senior-Software-Engineer-C--_R0122742) | LSEG Business Services RM S.R.L |
| LSEG | 4 | [Debt Capital Markets -Business Development](https://lseg.wd3.myworkdayjobs.com/Careers/job/New-York-United-States/Senior-Associate-Americas-Fixed-Income-Origination_R0120076-1) | DNU_London Stock Exchange Group Holdings Inc |
| London Stock Exchange Group | 9 | [Debt Capital Markets -Business Development](https://lseg.wd3.myworkdayjobs.com/Careers/job/New-York-United-States/Senior-Associate-Americas-Fixed-Income-Origination_R0120076-1) | DNU_London Stock Exchange Group Holdings Inc |

## Prudential, PGIM

Endpoint: `workday/pru.wd5.myworkdayjobs.com/pru/PGIM_Careers`. Queue rows: 71.

Board attribution: **Prudential and PGIM**. Prudential includes both Careers and PGIM_Careers in config; PGIM includes PGIM_Careers, which creates the collision. The Prudential sample identifies Prudential Ins Co of America and returns a canonical Careers URL. The PGIM sample identifies PGIM Real Estate Loan Services and describes PGIM as the asset management business of Prudential. This is a shared tenant with related employers, not an unrelated-company error. Workday can resolve a detail through a different site route, so the canonical URL is shown below; detail access alone does not prove membership in both listings.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Prudential | 45 | [Manager, Treasury - Payments & Cash Management](https://pru.wd5.myworkdayjobs.com/Careers/job/Newark-NJ-USA/Manager--Treasury---Payments---Cash-Management_R-124931-1) | 001 Prudential Ins Co of America |
| PGIM | 26 | [PGIM Real Estate-Senior Treasury Specialist (Hybrid-Dallas, TX)](https://pru.wd5.myworkdayjobs.com/PGIM_Careers/job/Dallas-TX-USA/PGIM-Real-Estate-Senior-Servicing-Specialist_R-125009) | 073 PGIM Real Estate Loan Services |

## State Street, Charles River Development

Endpoint: `workday/statestreet.wd1.myworkdayjobs.com/statestreet/Global`. Queue rows: 48.

Board attribution: **State Street**. Both posting descriptions identify State Street. The fallback sample under Charles River Development identifies State Street Technology (Ireland) Limited in hiringOrganization.name. This is a broad State Street board, not a board restricted to Charles River Development. Charles River Development has no queue rows, so its sample comes from the live shared listing.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| State Street | 48 | [Assistant Vice President, Collateral & Intraday Liquidity Management](https://statestreet.wd1.myworkdayjobs.com/Global/job/Boston-Massachusetts/Assistant-Vice-President--Collateral---Intraday-Liquidity-Management_R-796138-6) | 2001 SSB&T |
| Charles River Development | 0 | [Technology Financial Operations Data Analytics & BI Engineer](https://statestreet.wd1.myworkdayjobs.com/Global/job/Dublin-2-Ireland/Technology-Financial-Operations-Data-Analytics---BI-Engineer_R-796407) | 1392 State Street Technology (Ireland) Limited |

## TD Securities, TD Bank

Endpoint: `workday/td.wd3.myworkdayjobs.com/td/TD_Bank_Careers`. Queue rows: 90.

Board attribution: **TD, including TD Bank and TD Securities**. The TD Securities sample identifies TD Securities (USA) LLC and describes TD Securities capital markets work. The TD Bank sample identifies TD Bank, N.A. The same board serves multiple TD employers; either config name alone is too narrow to label every result.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| TD Securities | 42 | [Senior AML IT Developer](https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Toronto-Ontario/Senior-AML-IT-Developer_R_1514440) | TD Securities (USA) LLC |
| TD Bank | 48 | [Treasury Liquidity Analyst (US)](https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Charlotte-North-Carolina/Treasury-Liquidity-Analyst--US-_R_1511733) | TD Bank, N.A. |

## Nuveen, TIAA

Endpoint: `workday/tiaa.wd1.myworkdayjobs.com/tiaa/Search`. Queue rows: 24.

Board attribution: **TIAA shared board**. Both selected queue samples identify TIAA in hiringOrganization.name. The description under the Nuveen label describes TIAA and mentions Nuveen in group-wide culture language. Config searches for Nuveen, but this sample is a TIAA posting, so the search term does not establish Nuveen as the hiring employer. The shared board requires employer-level attribution.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Nuveen | 13 | [Sr Info Sec Threat Hunting Specialist](https://tiaa.wd1.myworkdayjobs.com/Search/job/Dallas-TX-USA/Sr-Info-Sec-Threat-Hunting-Specialist-II_R260900180-1) | TIAA |
| TIAA | 11 | [Mgr, Asset Liability Management](https://tiaa.wd1.myworkdayjobs.com/Search/job/Charlotte-NC-USA/Mgr--Asset-Liability-Management_R260900474-1) | TIAA |

## Westlake Financial, Westlake

Endpoint: `workday/westlake.wd1.myworkdayjobs.com/westlake/westlake`. Queue rows: 4.

Board attribution: **Westlake, with Westlake Management Services, Inc. as the sample employer**. Both names lead to the same Houston treasury requisition. The hiringOrganization.name field identifies Westlake Management Services, Inc.; the description references products, manufacturing experience and plant tours. Westlake Financial is incorrectly assigned to this board.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Westlake Financial | 2 | [Associate Director, Treasury Operations](https://westlake.wd1.myworkdayjobs.com/westlake/job/US---Houston-TX/Associate-Director--Treasury-Operations_R33624) | Westlake Management Services, Inc. |
| Westlake | 2 | [Associate Director, Treasury Operations](https://westlake.wd1.myworkdayjobs.com/westlake/job/US---Houston-TX/Associate-Director--Treasury-Operations_R33624) | Westlake Management Services, Inc. |

## Zillow Group, Zillow

Endpoint: `workday/zillow.wd5.myworkdayjobs.com/zillow/Zillow_Group_External`. Queue rows: 0.

Board attribution: **Zillow Group**. Both names lead to the same Studio Z posting. The description names Zillow Group; hiringOrganization.name identifies Zillow Mexico. Zillow and Zillow Group are aliases here. Both labels have zero queue rows, so the sample comes from the live shared listing.

| Config name | Queue rows | Live posting opened | Employer field |
| --- | ---: | --- | --- |
| Zillow Group | 0 | [Program Manager, Studio Z](https://zillow.wd5.myworkdayjobs.com/Zillow_Group_External/job/Mexico-City/Program-Manager--Studio-Z_P751334-2) | ZMEX Zillow Mexico, S. de R.L. de C.V. |
| Zillow | 0 | [Program Manager, Studio Z](https://zillow.wd5.myworkdayjobs.com/Zillow_Group_External/job/Mexico-City/Program-Manager--Studio-Z_P751334-2) | ZMEX Zillow Mexico, S. de R.L. de C.V. |

## Test and CI findings

Initial `py -m pytest -q test_validate_config.py test_liveness.py test_fetchers.py`: 109 passed, 0 failed. The liveness cases now use pytest functions, independent queue fixtures, fixed dates, and assertions rather than import-time checks and sys.exit.

After concurrent scout and fetcher-test edits appeared in the shared checkout, the same command reported 114 passed and 1 failed. `test_failed_alert_is_marked_pending_then_retried_then_dropped` at test_fetchers.py:179 expected alert_attempts=1 after the initial failed delivery but received 2. This is a failure in the concurrently edited scout snapshot; no scout or fetcher-test changes were made as part of this task.

The subsequent run after further concurrent edits passed all 115 tests. The alert failure above is therefore an observed intermediate failure, not an outstanding failure in that later shared-checkout snapshot. The validator and liveness tests alone passed all 99 cases.

`py validate_config.py`: expected exit 1, reporting the 10 shared-endpoint groups above. CI runs all three offline pytest files and the validator before fetching. A second validator step immediately before the scout protects the separately checked-out config; failed validation or tests block the scout, while a fetch-shard failure retains the existing partial-fetch behavior.
