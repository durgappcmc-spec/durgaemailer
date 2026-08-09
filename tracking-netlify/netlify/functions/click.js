// NOTE: 302 to target_url from Apps Script; falls back to CLICK_FALLBACK_URL.
import { clientIp, extractIdFromEvent, isLikelyBot, logToSheet } from "./_shared.js";

export async function handler(event) {
  const linkId = extractIdFromEvent(event, { kind: "click" });
  const headers = event.headers || {};
  const ua = headers["user-agent"] || headers["User-Agent"] || "";
  const ip = clientIp(event);
  const is_bot = isLikelyBot(ua);

  const data = linkId
    ? await logToSheet({
        action: "log_click",
        link_id: linkId,
        ip,
        user_agent: ua,
        is_bot,
        timestamp: new Date().toISOString(),
      })
    : null;

  if (!linkId) {
    console.error("[click] missing link_id", {
      path: event.path,
      rawUrl: event.rawUrl,
      qs: event.queryStringParameters,
    });
  }

  const target =
    (data && data.target_url) ||
    process.env.CLICK_FALLBACK_URL ||
    "https://karunamedia.org";

  return {
    statusCode: 302,
    headers: {
      Location: target,
      "Cache-Control": "no-store",
    },
    body: "",
  };
}
