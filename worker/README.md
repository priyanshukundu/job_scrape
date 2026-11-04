# JD proxy (Cloudflare Worker)

Backs the Copy JD button on the public Jobs Tracker page. Stateless: the page sends the ATS details
for one posting, the Worker fetches its description from the employer's public job API and returns
plain text. Free tier is 100k requests/day.

Deploy once:

    cd worker
    npx wrangler login
    npx wrangler deploy

The deployed URL is set as JD_PROXY_URL in job_scout.py (https://job-scout-jd-proxy.yazad-jobscout.workers.dev).

Only these hosts are ever fetched: boards-api.greenhouse.io, api.lever.co, api.eu.lever.co,
api.ashbyhq.com, api.smartrecruiters.com, `*.myworkdayjobs.com`, `*.oraclecloud.com`, plus, for every other ATS, only the https hosts listed for that company in the page's map (config host fields and the hosts of its queue links). Those are read as HTML (JSON-LD, then the description block); boards that render in the browser return a clear "copy it by hand" error.
