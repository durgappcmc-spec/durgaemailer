// NOTE: 302 to target_url from Apps Script; falls back to CLICK_FALLBACK_URL.
import { clientIp, isLikelyBot, logToSheet } from "./_shared.js";

export async function handler(event) {
  const linkId =
    (event.queryStringParameters && event.queryStringParameters.id) || "";
  const headers = event.headers || {};
  const ua = headers["user-agent"] || headers["User-Agent"] || "";
  const ip = clientIp(event);
  const is_bot = isLikelyBot(ua);

  const data = await logToSheet({
    action: "log_click",
    link_id: linkId,
    ip,
    user_agent: ua,
    is_bot,
    timestamp: new Date().toISOString(),
  });

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
