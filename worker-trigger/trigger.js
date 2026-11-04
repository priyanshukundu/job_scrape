// Starts the job-scout workflow on schedule; GitHub's own cron is too slow and stays as backup.
const REPO = "priyanshukundu/job_scrape";
const DISPATCH = `https://api.github.com/repos/${REPO}/actions/workflows/scout.yml/dispatches`;
const LAST_OK = `https://api.github.com/repos/${REPO}/actions/workflows/scout.yml/runs?status=success&per_page=1`;
const STALE_HOURS = 10;   // matches STALE_ALERT_HOURS on the tracker page
const REPEAT_HOURS = 6;   // re-alert cadence while still stale; stateless via the age window below

async function ntfy(env, title, message) {
  if (!env.NTFY_TOPIC) return;
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, { method: "POST", body: message, headers: { Title: title } });
}

const ghHeaders = (env) => ({
  Authorization: `Bearer ${env.GH_TOKEN}`,
  Accept: "application/vnd.github+json",
  "User-Agent": "job-scout-trigger",
  "X-GitHub-Api-Version": "2022-11-28",
});

async function dispatch(env) {
  const res = await fetch(DISPATCH, { method: "POST", headers: ghHeaders(env), body: JSON.stringify({ ref: "main" }) });
  if (res.status !== 204) {
    const detail = `${res.status} ${(await res.text()).slice(0, 200)}`;
    await ntfy(env, "job-scout trigger failed", `workflow_dispatch returned ${detail} (token expired or revoked?)`);
    throw new Error(detail);
  }
}

// Fires only in the hourly slot where the age crosses STALE_HOURS, then every REPEAT_HOURS, so a long outage does not spam.
async function checkStale(env) {
  const res = await fetch(LAST_OK, { headers: ghHeaders(env) });
  if (!res.ok) return;
  const run = (await res.json()).workflow_runs?.[0];
  if (!run) return;
  const age = (Date.now() - new Date(run.updated_at).getTime()) / 3600000;
  const over = age - STALE_HOURS;
  if (over >= 0 && over % REPEAT_HOURS < 1) {
    await ntfy(env, "job-scout stalled", `No successful scout run for ${Math.floor(age)} hours. Check GitHub Actions.`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(Promise.allSettled([dispatch(env), checkStale(env)]));
  },
};
