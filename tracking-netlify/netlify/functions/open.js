// NOTE: Returns a 1x1 transparent GIF; logs open to Apps Script.
import { clientIp, isLikelyBot, logToSheet } from "./_shared.js";

const PIXEL_B64 =
  "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==";
const PIXEL_BUFFER = Buffer.from(PIXEL_B64, "base64");

export async function handler(event) {
  let emailId = (event.queryStringParameters && event.queryStringParameters.id) || "";
  if (emailId.endsWith(".gif")) emailId = emailId.slice(0, -4);

  const headers = event.headers || {};
  const ua = headers["user-agent"] || headers["User-Agent"] || "";
  const ip = clientIp(event);
  const is_bot = isLikelyBot(ua);

  await logToSheet({
    action: "log_open",
    email_id: emailId,
    ip,
    user_agent: ua,
    is_bot,
    timestamp: new Date().toISOString(),
  });

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
