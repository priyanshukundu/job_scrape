// Port of job_scout.describe() for the ATSes in config.json. Keep the two in step.
const UA = { "User-Agent": "job-scout/1.0", Accept: "application/json" };
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const SAFE_SLUG = /^[A-Za-z0-9._-]+$/;
const NAMED = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", rsquo: "'", lsquo: "'", rdquo: '"', ldquo: '"', ndash: "-", mdash: "-", bull: "*", hellip: "..." };

function unescapeOnce(t) {
  return t.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, e) => {
    if (e[0] === "#") {
      const code = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return NAMED[e.toLowerCase()] ?? m;
  });
}

function stripHtml(text) {
  let t = unescapeOnce(unescapeOnce(text || "")); // Greenhouse double-escapes
  t = t.replace(/<[^>]+>/g, " ");
  return t.replace(/\s+/g, " ").trim();
}

async function getJson(url, init) {
  const r = await fetch(url, { headers: UA, ...init });
  if (!r.ok) throw new Error(`upstream returned ${r.status}`);
  return r.json();
}

function need(ok, msg) {
  if (!ok) throw new Error(msg);
}

async function describePage(cfg, link) {
  // Fallback for ATSes with no JSON API here: read the posting page itself, same host only, https only.
  let u;
  try { u = new URL(link); } catch { throw new Error("bad posting link"); }
  need(u.protocol === "https:" && Array.isArray(cfg.hosts) && cfg.hosts.includes(u.hostname), "posting link does not match the company host");
  if (u.hostname.endsWith(".icims.com")) u.searchParams.set("in_iframe", "1"); // iCIMS serves the JSON-LD only in its iframe view
  const r = await fetch(u.href, { headers: { "User-Agent": "Mozilla/5.0 job-scout/1.0", Accept: "text/html, */*" } });
  need(r.ok, `upstream returned ${r.status}`);
  const raw = (await r.text()).replace(/<(script|style)(?![^>]*ld\+json)[^>]*>[\s\S]*?<\/>/gi, (m) => m);
  for (const m of raw.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/gi)) {
    try {
      const d = JSON.parse(m[1]);
      const list = Array.isArray(d) ? d : [d];
      for (const x of list) if (x && x.description) return stripHtml(x.description).slice(0, 30000);
    } catch { /* next block */ }
  }
  const noScript = raw.replace(/<(script|style)[\s\S]*?<\/>/gi, " ");
  const i = noScript.indexOf('itemprop="description"');
  if (i >= 0) return stripHtml(noScript.slice(noScript.indexOf(">", i) + 1)).slice(0, 30000);
  const mm = noScript.match(/<main[\s\S]*?<\/main>/i);
  const t = stripHtml(mm ? mm[0] : "").slice(0, 30000);
  need(t.length > 200, "this ATS renders the description in the browser; open the link and copy it by hand");
  return t;
}

async function describe(cfg, jid, link) {
  const { ats } = cfg;
  if (ats === "page") return describePage(cfg, link);
  if (ats === "greenhouse") {
    need(SAFE_SLUG.test(cfg.slug) && /^\d+$/.test(jid), "bad greenhouse ref");
    const d = await getJson(`https://boards-api.greenhouse.io/v1/boards/${cfg.slug}/jobs/${jid}`);
    return stripHtml(d.content);
  }
  if (ats === "lever") {
    need(SAFE_SLUG.test(cfg.slug) && SAFE_SLUG.test(jid), "bad lever ref");
    const host = cfg.region === "eu" ? "api.eu.lever.co" : "api.lever.co";
    const j = await getJson(`https://${host}/v0/postings/${cfg.slug}/${jid}?mode=json`);
    let desc = j.descriptionPlain || stripHtml(j.description);
    for (const l of j.lists || []) desc += ` ${l.text || ""} ${stripHtml(l.content)}`;
    return `${desc} ${j.additionalPlain || ""}`.trim();
  }
  if (ats === "ashby") {
    need(SAFE_SLUG.test(cfg.slug), "bad ashby ref");
    const d = await getJson(`https://api.ashbyhq.com/posting-api/job-board/${cfg.slug}`);
    const j = (d.jobs || []).find((x) => String(x.id) === jid);
    return j ? j.descriptionPlain || stripHtml(j.descriptionHtml) : "";
  }
  if (ats === "smartrecruiters") {
    need(SAFE_SLUG.test(cfg.slug) && SAFE_SLUG.test(jid), "bad smartrecruiters ref");
    const d = await getJson(`https://api.smartrecruiters.com/v1/companies/${cfg.slug}/postings/${jid}`);
    const secs = (d.jobAd && d.jobAd.sections) || {};
    return stripHtml(Object.values(secs).map((s) => (s || {}).text || "").join(" "));
  }
  if (ats === "workday") {
    need(/^[a-z0-9-]+(\.[a-z0-9-]+)*\.myworkdayjobs\.com$/i.test(cfg.host), "bad workday host");
    need(SAFE_SLUG.test(cfg.tenant) && jid.startsWith("/job/") && !jid.includes(".."), "bad workday ref");
    let err;
    for (const site of cfg.sites || []) {
      need(SAFE_SLUG.test(site), "bad workday site");
      try {
        const d = await getJson(`https://${cfg.host}/wday/cxs/${cfg.tenant}/${site}${jid}`);
        return stripHtml((d.jobPostingInfo || {}).jobDescription);
      } catch (e) {
        err = e;
      }
    }
    throw err || new Error("no workday site configured");
  }
  if (ats === "oracle") {
    need(/^[a-z0-9-]+(\.[a-z0-9-]+)*\.oraclecloud\.com$/i.test(cfg.host), "bad oracle host");
    need(/^\d+$/.test(jid), "bad oracle ref");
    const qs = new URLSearchParams({ onlyData: "true", finder: `ById;Id=${jid}` });
    const d = await getJson(`https://${cfg.host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?${qs}`);
    const it = (d.items || [])[0];
    if (!it) return "";
    return stripHtml(["ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"].map((k) => it[k]).filter(Boolean).join(" "));
  }
  throw new Error(`unsupported ATS: ${ats}`);
}

function reply(code, payload) {
  return new Response(JSON.stringify(payload), { status: code, headers: { ...CORS, "Content-Type": "application/json" } });
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "POST") return reply(405, { ok: false, stderr: "POST only" });
    try {
      const b = await request.json();
      if (!b.cfg || (!b.jid && b.cfg.ats !== "page")) return reply(200, { ok: false, stderr: "posting has no ATS details on this page" });
      const text = await describe(b.cfg, String(b.jid), String(b.link || ""));
      if (!text) return reply(200, { ok: false, stderr: "posting is no longer live or its description is empty" });
      const head = [b.role, b.company, `Location: ${b.location || ""}`, `Link: ${b.link || ""}`].join("\n");
      return reply(200, { ok: true, text: `${head}\n\n${text}` });
    } catch (e) {
      return reply(200, { ok: false, stderr: String(e.message || e) });
    }
  },
};
