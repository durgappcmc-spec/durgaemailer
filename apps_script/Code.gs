/**
 * Relay — Apps Script scheduler + Gmail sender + reply watcher + tracking logger.
 *
 * NOTE: SHEET_ID / TRACKING_BASE are filled by setup scripts or manually after deploy.
 * Primary open pixel + click 302 are served by Netlify; this Web App still handles
 * register/schedule/cancel/list/log_* and optional HTML fallbacks for doGet.
 */

// ==== FILL THESE IN ====
const SHEET_ID = "1FkOrjF2h0kUA2KkAVnL21ZXo5U5SltTOdVkPDKCYSXs";
const TRACKING_BASE = "https://durgaemailer-tracking.netlify.app";
// =======================

const SENDS_SHEET = "Sends";
const OPENS_SHEET = "Opens";
const CLICKS_SHEET = "Clicks";
const LINKS_SHEET = "Links";
const SCHEDULED_SHEET = "Scheduled";
const REPLIES_SHEET = "Replies";

function setup() {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  ensureSheet_(ss, SCHEDULED_SHEET, [
    "queued_at", "send_at", "status", "recipient_email", "recipient_name",
    "subject", "html_body", "campaign", "source", "attachments_json",
    "email_id", "error", "attempts", "thread_id", "gmail_message_id",
  ]);
  ensureSheet_(ss, SENDS_SHEET, [
    "sent_at", "email_id", "recipient_email", "recipient_name", "subject",
    "campaign", "source", "thread_id", "gmail_message_id",
  ]);
  ensureSheet_(ss, OPENS_SHEET, [
    "opened_at", "email_id", "ip", "user_agent", "is_bot", "is_first_open",
  ]);
  ensureSheet_(ss, CLICKS_SHEET, [
    "clicked_at", "link_id", "email_id", "ip", "user_agent", "is_bot",
  ]);
  ensureSheet_(ss, LINKS_SHEET, [
    "link_id", "email_id", "original_url", "label",
  ]);
  ensureSheet_(ss, REPLIES_SHEET, [
    "detected_at", "recipient_email", "thread_id", "original_email_id",
    "original_campaign", "reply_snippet", "cancelled_count",
  ]);
}

function ensureSheet_(ss, name, headers) {
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  const existing = sh.getRange(1, 1, 1, headers.length).getValues()[0];
  const blank = existing.every(function (v) { return v === "" || v === null; });
  if (blank) {
    sh.getRange(1, 1, 1, headers.length).setValues([headers]);
    sh.setFrozenRows(1);
  }
}

function doGet(e) {
  e = e || { parameter: {} };
  const a = (e.parameter && e.parameter.a) || "";
  if (a === "o") {
    // Apps Script cannot return raw GIF bytes; Netlify serves the real pixel.
    return HtmlService.createHtmlOutput("ok");
  }
  if (a === "c") {
    const linkId = (e.parameter && e.parameter.id) || "";
    const target = lookupLinkUrl_(linkId) || TRACKING_BASE || "https://karunamedia.org";
    return HtmlService.createHtmlOutput(
      '<html><head><meta http-equiv="refresh" content="0;url=' +
        target.replace(/"/g, "") +
        '"></head><body>Redirecting…</body></html>'
    );
  }
  return jsonOut({ ok: true, service: "relay-apps-script" });
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    const action = body.action || "";
    switch (action) {
      case "register":
        return jsonOut(handleRegister(body));
      case "schedule":
        return jsonOut(handleSchedule(body));
      case "cancel":
        return jsonOut(handleCancel(body));
      case "list":
        return jsonOut(handleList(body));
      case "list_replies":
        return jsonOut(handleListReplies(body));
      case "log_open":
        return jsonOut(handleLogOpen(body));
      case "log_click":
        return jsonOut(handleLogClick(body));
      default:
        return jsonOut({ ok: false, error: "unknown action: " + action });
    }
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

function handleRegister(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const links = body.links || [];
  if (links.length) {
    const sh = ss.getSheetByName(LINKS_SHEET);
    const rows = links.map(function (l) {
      return [l.link_id, body.email_id, l.original_url || "", l.label || ""];
    });
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, 4).setValues(rows);
  }
  // Seed Sends so opens/clicks from Relay drafts (sent later in Gmail) join in Tracking
  if (body.email_id) {
    const sends = ss.getSheetByName(SENDS_SHEET);
    const data = sends.getDataRange().getValues();
    const headers = data[0] || [];
    const iEid = headers.indexOf("email_id");
    let exists = false;
    if (iEid >= 0) {
      for (let r = 1; r < data.length; r++) {
        if (String(data[r][iEid]) === String(body.email_id)) {
          exists = true;
          break;
        }
      }
    }
    if (!exists) {
      const src =
        body.source ||
        body.prospect_source ||
        "relay_draft";
      sends.appendRow([
        new Date().toISOString(),
        body.email_id,
        body.recipient_email || "",
        body.recipient_name || "",
        body.subject || "",
        body.campaign || "",
        src,
        "",
        "",
      ]);
    }
  }
  return { ok: true, email_id: body.email_id, links: links.length, seeded_send: true };
}

function handleSchedule(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const sh = ss.getSheetByName(SCHEDULED_SHEET);
  const jobs = body.jobs ? body.jobs : [body];
  const now = new Date().toISOString();
  const rows = jobs.map(function (job) {
    return [
      now,
      job.send_at || "",
      "pending",
      job.recipient_email || "",
      job.recipient_name || "",
      job.subject || "",
      job.html_body || "",
      job.campaign || "",
      job.source || "",
      JSON.stringify(job.attachments || []),
      "",
      "",
      0,
      "",
      "",
    ];
  });
  if (rows.length) {
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
  }
  return { ok: true, queued: rows.length };
}

function handleCancel(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const sh = ss.getSheetByName(SCHEDULED_SHEET);
  const data = sh.getDataRange().getValues();
  const headers = data[0];
  const iStatus = headers.indexOf("status");
  const iEmail = headers.indexOf("recipient_email");
  const iCampaign = headers.indexOf("campaign");
  const iError = headers.indexOf("error");
  let cancelled = 0;
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][iStatus]) !== "pending") continue;
    if (body.recipient_email && String(data[r][iEmail]).toLowerCase() !== String(body.recipient_email).toLowerCase()) continue;
    if (body.campaign && String(data[r][iCampaign]) !== String(body.campaign)) continue;
    sh.getRange(r + 1, iStatus + 1).setValue("cancelled");
    sh.getRange(r + 1, iError + 1).setValue("cancelled via API");
    cancelled++;
  }
  return { ok: true, cancelled: cancelled };
}

function handleList(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const what = body.what || "scheduled";
  let name = SCHEDULED_SHEET;
  if (what === "sends") name = SENDS_SHEET;
  else if (what === "opens") name = OPENS_SHEET;
  else if (what === "clicks") name = CLICKS_SHEET;
  else if (what === "links") name = LINKS_SHEET;
  else if (what === "replies") name = REPLIES_SHEET;
  const sh = ss.getSheetByName(name);
  const values = sh.getDataRange().getValues();
  if (values.length < 2) return { ok: true, rows: [] };
  const headers = values[0];
  let rows = values.slice(1).map(function (row) {
    const obj = {};
    headers.forEach(function (h, i) { obj[h] = row[i]; });
    return obj;
  });
  if (body.status) {
    rows = rows.filter(function (r) { return String(r.status) === String(body.status); });
  }
  if (body.campaign) {
    rows = rows.filter(function (r) { return String(r.campaign || "") === String(body.campaign); });
  }
  if (body.exclude_bots) {
    rows = rows.filter(function (r) {
      return !(r.is_bot === true || r.is_bot === "TRUE" || r.is_bot === "true");
    });
  }
  return { ok: true, rows: rows };
}

function handleLogOpen(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const sh = ss.getSheetByName(OPENS_SHEET);
  const emailId = body.email_id || "";
  const isBot = !!body.is_bot;
  const isFirst = !isBot && !hasPriorNonBotOpen_(ss, emailId);
  sh.appendRow([
    body.timestamp || new Date().toISOString(),
    emailId,
    body.ip || "",
    body.user_agent || "",
    isBot,
    isFirst,
  ]);
  return { ok: true, is_first_open: isFirst };
}

function hasPriorNonBotOpen_(ss, emailId) {
  const sh = ss.getSheetByName(OPENS_SHEET);
  const data = sh.getDataRange().getValues();
  const headers = data[0];
  const iEid = headers.indexOf("email_id");
  const iBot = headers.indexOf("is_bot");
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][iEid]) !== String(emailId)) continue;
    const bot = data[r][iBot];
    if (!(bot === true || bot === "TRUE" || bot === "true")) return true;
  }
  return false;
}

function handleLogClick(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const linkId = body.link_id || "";
  const found = lookupLinkRow_(ss, linkId);
  const emailId = found ? found.email_id : "";
  const target = found ? found.original_url : "";
  const sh = ss.getSheetByName(CLICKS_SHEET);
  sh.appendRow([
    body.timestamp || new Date().toISOString(),
    linkId,
    emailId,
    body.ip || "",
    body.user_agent || "",
    !!body.is_bot,
  ]);
  return { ok: true, target_url: target || TRACKING_BASE };
}

function lookupLinkRow_(ss, linkId) {
  const sh = ss.getSheetByName(LINKS_SHEET);
  const data = sh.getDataRange().getValues();
  const headers = data[0];
  const iId = headers.indexOf("link_id");
  const iEid = headers.indexOf("email_id");
  const iUrl = headers.indexOf("original_url");
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][iId]) === String(linkId)) {
      return { email_id: data[r][iEid], original_url: data[r][iUrl] };
    }
  }
  return null;
}

function lookupLinkUrl_(linkId) {
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const row = lookupLinkRow_(ss, linkId);
    return row ? row.original_url : "";
  } catch (e) {
    return "";
  }
}

function handleListReplies(body) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const sh = ss.getSheetByName(REPLIES_SHEET);
  const values = sh.getDataRange().getValues();
  if (values.length < 2) return { ok: true, rows: [] };
  const headers = values[0];
  let rows = values.slice(1).map(function (row) {
    const obj = {};
    headers.forEach(function (h, i) { obj[h] = row[i]; });
    return obj;
  });
  if (body.days) {
    const cutoff = Date.now() - Number(body.days) * 86400000;
    rows = rows.filter(function (r) {
      const t = Date.parse(r.detected_at);
      return !isNaN(t) && t >= cutoff;
    });
  }
  rows.sort(function (a, b) {
    return Date.parse(b.detected_at) - Date.parse(a.detected_at);
  });
  return { ok: true, rows: rows };
}

function processScheduledEmails() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sh = ss.getSheetByName(SCHEDULED_SHEET);
    const data = sh.getDataRange().getValues();
    if (data.length < 2) return;
    const headers = data[0];
    const idx = {};
    headers.forEach(function (h, i) { idx[h] = i; });
    const now = new Date();
    let processed = 0;
    for (let r = 1; r < data.length && processed < 20; r++) {
      const row = data[r];
      if (String(row[idx.status]) !== "pending") continue;
      const sendAt = new Date(row[idx.send_at]);
      if (isNaN(sendAt.getTime()) || sendAt > now) continue;

      sh.getRange(r + 1, idx.status + 1).setValue("sending");
      const attempts = Number(row[idx.attempts] || 0) + 1;
      sh.getRange(r + 1, idx.attempts + 1).setValue(attempts);

      const job = {
        recipient_email: row[idx.recipient_email],
        recipient_name: row[idx.recipient_name],
        subject: row[idx.subject],
        html_body: row[idx.html_body],
        campaign: row[idx.campaign],
        source: row[idx.source],
        attachments_json: row[idx.attachments_json],
      };
      try {
        const result = sendOneEmail(job);
        sh.getRange(r + 1, idx.status + 1).setValue("sent");
        sh.getRange(r + 1, idx.email_id + 1).setValue(result.emailId || "");
        sh.getRange(r + 1, idx.thread_id + 1).setValue(result.threadId || "");
        sh.getRange(r + 1, idx.gmail_message_id + 1).setValue(result.gmailMsgId || "");
        sh.getRange(r + 1, idx.error + 1).setValue("");
      } catch (err) {
        const status = attempts >= 3 ? "failed" : "pending";
        sh.getRange(r + 1, idx.status + 1).setValue(status);
        sh.getRange(r + 1, idx.error + 1).setValue(String(err));
      }
      processed++;
    }
  } finally {
    lock.releaseLock();
  }
}

function sendOneEmail(job) {
  const emailId = Utilities.getUuid();
  const instrumented = instrumentHtmlServerSide(job.html_body || "", emailId);
  const ss = SpreadsheetApp.openById(SHEET_ID);
  if (instrumented.linkRows.length) {
    const linkSh = ss.getSheetByName(LINKS_SHEET);
    linkSh.getRange(
      linkSh.getLastRow() + 1,
      1,
      instrumented.linkRows.length,
      4
    ).setValues(instrumented.linkRows);
  }

  const attachments = [];
  try {
    const parsed = JSON.parse(job.attachments_json || "[]");
    (parsed || []).forEach(function (a) {
      if (a && a.driveFileId) {
        const file = DriveApp.getFileById(a.driveFileId);
        attachments.push(file.getBlob().setName(a.name || file.getName()));
      } else if (a && a.data_base64) {
        const bytes = Utilities.base64Decode(a.data_base64);
        const blob = Utilities.newBlob(
          bytes,
          a.mimeType || a.mime_type || "application/octet-stream",
          a.name || "attachment"
        );
        attachments.push(blob);
      }
    });
  } catch (e) {
    // ignore bad attachments json
  }

  GmailApp.sendEmail(
    job.recipient_email,
    job.subject || "(no subject)",
    "HTML email — please view in an HTML-capable client.",
    {
      htmlBody: instrumented.html,
      name: job.recipient_name || undefined,
      attachments: attachments,
    }
  );

  let threadId = "";
  let gmailMsgId = "";
  const me = Session.getActiveUser().getEmail();
  for (let attempt = 0; attempt < 3; attempt++) {
    Utilities.sleep(1500);
    const q =
      'to:' + job.recipient_email + ' subject:"' + (job.subject || "").replace(/"/g, "") + '" newer_than:1d';
    const threads = GmailApp.search(q, 0, 5);
    if (threads && threads.length) {
      const thr = threads[0];
      threadId = thr.getId();
      const msgs = thr.getMessages();
      const last = msgs[msgs.length - 1];
      gmailMsgId = last.getHeader("Message-Id") || last.getId();
      break;
    }
  }

  ss.getSheetByName(SENDS_SHEET).appendRow([
    new Date().toISOString(),
    emailId,
    job.recipient_email,
    job.recipient_name || "",
    job.subject || "",
    job.campaign || "",
    job.source || "",
    threadId,
    gmailMsgId,
  ]);

  return { emailId: emailId, threadId: threadId, gmailMsgId: gmailMsgId };
}

function instrumentHtmlServerSide(html, emailId) {
  const linkRows = [];
  const rewritten = String(html || "").replace(
    /<a\s+([^>]*?)href\s*=\s*(["'])(.*?)\2([^>]*)>/gi,
    function (match, pre, quote, href, post) {
      const h = String(href || "").trim();
      const lower = h.toLowerCase();
      if (!h || lower.indexOf("mailto:") === 0 || lower.indexOf("tel:") === 0 || h.charAt(0) === "#") {
        return match;
      }
      if (TRACKING_BASE && h.indexOf(TRACKING_BASE) !== -1) return match;
      const linkId = Utilities.getUuid();
      linkRows.push([linkId, emailId, h, ""]);
      return '<a ' + pre + 'href=' + quote + TRACKING_BASE + '/.netlify/functions/click?id=' + linkId + quote + post + '>';
    }
  );
  const pixel =
    '<img src="' + TRACKING_BASE + '/.netlify/functions/open?id=' + emailId +
    '" width="1" height="1" alt="" style="display:block;border:0;">';
  let out = rewritten;
  if (/<\/body>/i.test(out)) {
    out = out.replace(/<\/body>/i, pixel + "</body>");
  } else {
    out = out + pixel;
  }
  return { html: out, linkRows: linkRows };
}

function watchReplies() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const sends = ss.getSheetByName(SENDS_SHEET).getDataRange().getValues();
    if (sends.length < 2) return;
    const headers = sends[0];
    const idx = {};
    headers.forEach(function (h, i) { idx[h] = i; });

    const repliesSh = ss.getSheetByName(REPLIES_SHEET);
    const replyData = repliesSh.getDataRange().getValues();
    const knownThreads = {};
    if (replyData.length > 1) {
      const rh = replyData[0];
      const iTid = rh.indexOf("thread_id");
      for (let r = 1; r < replyData.length; r++) {
        knownThreads[String(replyData[r][iTid])] = true;
      }
    }

    // Most recent send per thread
    const byThread = {};
    for (let r = 1; r < sends.length; r++) {
      const tid = String(sends[r][idx.thread_id] || "");
      if (!tid) continue;
      const sentAt = new Date(sends[r][idx.sent_at]);
      if (!byThread[tid] || sentAt > byThread[tid].sentAt) {
        byThread[tid] = {
          sentAt: sentAt,
          recipient: sends[r][idx.recipient_email],
          emailId: sends[r][idx.email_id],
          campaign: sends[r][idx.campaign],
          threadId: tid,
        };
      }
    }

    const me = Session.getActiveUser().getEmail().toLowerCase();
    const scheduled = ss.getSheetByName(SCHEDULED_SHEET);

    Object.keys(byThread).forEach(function (tid) {
      if (knownThreads[tid]) return;
      const info = byThread[tid];
      let thread;
      try {
        thread = GmailApp.getThreadById(tid);
      } catch (e) {
        return;
      }
      if (!thread) return;
      const messages = thread.getMessages();
      for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];
        const from = (msg.getFrom() || "").toLowerCase();
        if (from.indexOf(me) !== -1) continue;
        if (from.indexOf(String(info.recipient || "").toLowerCase()) === -1) continue;
        if (msg.getDate() <= info.sentAt) continue;

        const snippet = (msg.getPlainBody() || "").substring(0, 200);
        if (isAutoResponse(msg)) {
          repliesSh.appendRow([
            new Date().toISOString(),
            info.recipient,
            tid,
            info.emailId,
            info.campaign,
            "[auto-reply] " + snippet,
            0,
          ]);
          knownThreads[tid] = true;
          return;
        }
        const cancelled = cancelPendingForRecipient(scheduled, info.recipient, info.campaign);
        repliesSh.appendRow([
          new Date().toISOString(),
          info.recipient,
          tid,
          info.emailId,
          info.campaign,
          snippet,
          cancelled,
        ]);
        knownThreads[tid] = true;
        return;
      }
    });
  } finally {
    lock.releaseLock();
  }
}

function isAutoResponse(msg) {
  const subject = msg.getSubject() || "";
  if (/out of office|out-of-office|auto[-\s]?reply|automatic reply|away from|vacation/i.test(subject)) {
    return true;
  }
  try {
    const autoSubmitted = msg.getHeader("Auto-Submitted") || "";
    if (/^auto-/i.test(autoSubmitted)) return true;
    const precedence = msg.getHeader("Precedence") || "";
    if (/(bulk|junk|auto)/i.test(precedence)) return true;
  } catch (e) {
    // headers may be unavailable
  }
  return false;
}

function cancelPendingForRecipient(sheet, recipient, campaign) {
  const data = sheet.getDataRange().getValues();
  const headers = data[0];
  const iStatus = headers.indexOf("status");
  const iEmail = headers.indexOf("recipient_email");
  const iCampaign = headers.indexOf("campaign");
  const iError = headers.indexOf("error");
  let count = 0;
  for (let r = 1; r < data.length; r++) {
    if (String(data[r][iStatus]) !== "pending") continue;
    if (String(data[r][iEmail]).toLowerCase() !== String(recipient || "").toLowerCase()) continue;
    if (campaign && String(data[r][iCampaign]) !== String(campaign)) continue;
    sheet.getRange(r + 1, iStatus + 1).setValue("cancelled");
    sheet.getRange(r + 1, iError + 1).setValue("auto-paused: reply detected");
    count++;
  }
  return count;
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "processScheduledEmails") {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger("processScheduledEmails").timeBased().everyMinutes(1).create();
}

function installReplyWatcher() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === "watchReplies") {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger("watchReplies").timeBased().everyMinutes(5).create();
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}

/*
==== DEPLOY STEPS ====
1) Fill SHEET_ID and TRACKING_BASE constants at the top of this file.
2) Run setup() once (creates all six tabs with headers).
3) Run installTrigger() (1-min scheduler).
4) Run installReplyWatcher() (5-min reply watcher).
5) Deploy → New deployment → Web App → Execute as: Me, Who has access: Anyone
   → copy the Web App URL into .env as APPS_SCRIPT_TRACKING_URL
   → also set Netlify env APPS_SCRIPT_LOG_URL to the same URL.
*/
