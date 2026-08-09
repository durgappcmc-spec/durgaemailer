/**
 * Inject RELAY_APP_URL from Netlify env into the portal HTML at build time.
 * Set Site env: RELAY_APP_URL=https://your-app.streamlit.app
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const htmlPath = path.join(__dirname, "public", "index.html");
const url = (process.env.RELAY_APP_URL || "").trim().replace(/\/$/, "");

let html = fs.readFileSync(htmlPath, "utf8");
if (url) {
  html = html.replaceAll("__RELAY_APP_URL__", url);
  console.log(`[relay-portal] injected RELAY_APP_URL=${url}`);
} else {
  console.warn(
    "[relay-portal] RELAY_APP_URL not set — portal will show configure message"
  );
}
fs.writeFileSync(htmlPath, html);
