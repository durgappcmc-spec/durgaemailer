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
