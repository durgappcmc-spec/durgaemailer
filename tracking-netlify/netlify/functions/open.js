// NOTE: Returns a 1x1 transparent GIF; logs open to Apps Script.
import { clientIp, extractIdFromEvent, isLikelyBot, logToSheet } from "./_shared.js";

const PIXEL_B64 =
  "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==";
const PIXEL_BUFFER = Buffer.from(PIXEL_B64, "base64");

export async function handler(event) {
  let emailId = extractIdFromEvent(event, { kind: "open" });

  const headers = event.headers || {};
  const ua = headers["user-agent"] || headers["User-Agent"] || "";
  const ip = clientIp(event);
  const is_bot = isLikelyBot(ua, { kind: "open" });

  if (emailId) {
    await logToSheet({
      action: "log_open",
      email_id: emailId,
      ip,
      user_agent: ua,
      is_bot,
      timestamp: new Date().toISOString(),
    });
  } else {
    console.error("[open] missing email_id", {
      path: event.path,
      rawUrl: event.rawUrl,
      qs: event.queryStringParameters,
    });
  }

  return {
    statusCode: 200,
    isBase64Encoded: true,
    headers: {
      "Content-Type": "image/gif",
      "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate",
      Pragma: "no-cache",
      Expires: "0",
    },
    body: PIXEL_BUFFER.toString("base64"),
  };
}
