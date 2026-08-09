// NOTE: Shared helpers for open/click Netlify functions. Node 18+ global fetch.
const BOT_RE =
  /GoogleImageProxy|YahooMailProxy|Mail\/\d|iPhone.*Mail|Macintosh.*Mail|Barracuda|Proofpoint|Mimecast|Symantec|MessageLabs|bot|crawler|spider|scanner|curl|wget|python-requests/i;

export function isLikelyBot(ua) {
  if (!ua) return true;
  return BOT_RE.test(ua);
}

export function clientIp(event) {
  const headers = event.headers || {};
  const xff = headers["x-forwarded-for"] || headers["X-Forwarded-For"];
  if (xff) return String(xff).split(",")[0].trim();
  return (
    headers["x-nf-client-connection-ip"] ||
    headers["X-Nf-Client-Connection-Ip"] ||
    ""
  );
}

function cleanId(raw) {
  if (!raw) return "";
  let id = decodeURIComponent(String(raw)).trim();
  // Drop anything after whitespace/newline (path+rawUrl concat hazard)
  id = id.split(/\s+/)[0];
  if (id.endsWith(".gif")) id = id.slice(0, -4);
  return id;
}

/**
 * Pull tracking id from query (?id=) or path (/t/o/{id}.gif, /t/c/{id}).
 * Netlify splat rewrites sometimes drop query params — path parsing is required.
 * Parse path and rawUrl separately so [^/?#] never eats across a newline.
 */
export function extractIdFromEvent(event, { kind = "open" } = {}) {
  const q = event.queryStringParameters || {};
  const fromQ = cleanId(q.id || q.email_id || q.link_id || "");
  if (fromQ) return fromQ;

  const candidates = [
    String(event.path || ""),
    String(event.rawPath || ""),
    String(event.rawUrl || ""),
  ];
  const pathRe =
    kind === "click" ? /\/t\/c\/([^/?#\s]+)/i : /\/t\/o\/([^/?#\s]+)/i;
  for (const src of candidates) {
    if (!src) continue;
    const m = src.match(pathRe);
    if (m) return cleanId(m[1]);
    const qm = src.match(/[?&]id=([^&#\s]+)/i);
    if (qm) return cleanId(qm[1]);
  }
  return "";
}

/** Fire-and-forget POST to Apps Script with a 5s timeout. */
export async function logToSheet(payload) {
  const url = process.env.APPS_SCRIPT_LOG_URL;
  if (!url) {
    console.error("[apps_script] APPS_SCRIPT_LOG_URL not set");
    return null;
  }
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(5000),
    });
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return { ok: resp.ok, raw: text };
    }
  } catch (err) {
    console.error("[apps_script] logToSheet failed:", err);
    return null;
  }
}
