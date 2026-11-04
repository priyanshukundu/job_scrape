# Read-only scout review

Author: Yazad Madan
Reviewed: October 3, 2026
Scope: `git diff 82cebc8^..7db761b -- job_scout.py test_fetchers.py`.

Findings and line numbers below refer to the immutable `7db761b` snapshot. Later concurrent changes are outside this review. No scout or existing fetcher-test source was changed.

## Findings

1. **High: prune can delete a role before closure is confirmed.** `job_scout.py:2446`, called at `job_scout.py:2242`, removes an old `new` or `auto_blocked` row whenever its company returned a complete nonempty listing and the row was not seen today. It does not require `closed_on` or sufficient misses. A role posted in January, confirmed live yesterday, and missing for the first time today survives the closure gate at `job_scout.py:2427` but is immediately pruned. Its ID remains in state, so a later reappearance does not normally requeue it. `test_fetchers.py:101` exercises the prune helper without composing it with the closure gate; it does not catch this loss. Regression: one old queued role last seen yesterday, zero prior misses, and one unrelated live ID; run the scout and require the old row to remain with miss_count=1 and closed_on=None. This fails at `7db761b`.

2. **Medium: the alert cap budgets successful deliveries, not attempts.** `job_scout.py:2345` and `job_scout.py:2368` compare counts["alerted"], which only increments after success. Three fresh roles with cap=2 and a failing notifier cause three individual attempts. A dead notification endpoint can therefore receive attempts for every fresh role and every eligible pending row, consuming the run's network time despite the cap. Regression: retain 20 seen jobs, add three fresh jobs, make notify return False, set the cap to two, and require at most two individual notification calls. This fails at `7db761b`; summaries are excluded from that assertion.

3. **Medium: cap exhaustion stops cleanup of later pending rows.** `job_scout.py:2369` breaks the retry loop when the first eligible row hits the cap. Closed, acted-on, or exhausted rows later in the queue never reach the cleanup at `job_scout.py:2365`. Their pending flags remain stale, and a later reopen can make an old pending alert eligible again. Regression: put one eligible pending row before a closed, applied, or exhausted pending row, set alerted equal to the cap, require zero sends, preserve the eligible pending flag, and clear the inactive flag. All three cases fail at `7db761b`.

4. **Medium: failed flood/overflow summaries are permanently lost.** `job_scout.py:2383` ignores notify's False result. Flooded and capped roles have no pending flag, and `job_scout.py:2354` still records all their IDs as seen. Reproduction at the reviewed snapshot: 15 fresh matches for an existing company produce one failed Summary call, zero pending flags, and zero notification calls in the next run after delivery recovers. Failing test idea: run that two-run sequence and require a durable summary or equivalent notification to be delivered in the second run. This was reproduced separately; no production retry design was imposed by a new test.

## Other requested paths

Missing shard results, explicit fetch errors, incomplete listings and empty listings all remain unverified at `job_scout.py:2184`; their old queue rows are protected from pruning. Empty first fetches do not seed state. Dry runs from shard files skip timing writes and persistence and print notifications without sending HTTP requests. Six ordinary regression cases covering these paths pass. A malformed shard JSON file can still abort loading globally, but that behavior predates the reviewed change.

The flood floor protects boards with ten or fewer fresh matches. On a small board with 12 matching roles, 11 legitimately new roles still trigger the heuristic. A partial first fetch can also seed state before full coverage is available, and the remaining baseline can trigger the flood guard on the next run. These are policy tradeoffs, not proof that the IDs changed. No failing test was added to contradict the explicitly chosen heuristic. The failed-summary finding above makes such false positives materially worse.

The closure date gate is a calendar-day gate, not an elapsed 24-hour gate. A sighting before midnight can satisfy it shortly after midnight once there are two complete misses. This matches the comparison in the code and is not presented as a separate implementation bug.

## Reproductions and verification

`python -m pytest -q review_tests/scout_regressions.py` fails five cases at `7db761b`: finding 1, finding 2, and the three finding 3 variants. The assertions state desired behavior; they contain no unconditional failures or expected-failure markers. All five pass with corrected behavior applied only in memory in an isolated positive-control process. No job_scout.py file was edited, including in the review worktree.

The reproduction filename is deliberately outside normal pytest discovery. CI runs the ordinary guard tests and health tests, while the known-failure reproductions require the explicit command above. Once corrected, these same assertions can be promoted to normal CI coverage.
